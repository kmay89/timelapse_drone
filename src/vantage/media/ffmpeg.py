"""Thin, typed wrappers around ffprobe / ffmpeg (invoked as subprocesses).

Everything that touches video goes through here so the rest of the code never
builds ffmpeg command lines by hand.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import numpy as np

from vantage import log


class FFmpegError(RuntimeError):
    pass


@cache
def ffmpeg_bin() -> str:
    path = shutil.which("ffmpeg")
    if not path:
        raise FFmpegError("ffmpeg not found on PATH (install it: apt install ffmpeg / brew install ffmpeg)")
    return path


@cache
def ffprobe_bin() -> str:
    path = shutil.which("ffprobe")
    if not path:
        raise FFmpegError("ffprobe not found on PATH (it ships with ffmpeg)")
    return path


@cache
def encoders() -> frozenset[str]:
    out = subprocess.run(
        [ffmpeg_bin(), "-hide_banner", "-encoders"], capture_output=True, text=True, check=True
    ).stdout
    names = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and len(parts[0]) == 6 and parts[0][0] in "VAS":
            names.add(parts[1])
    return frozenset(names)


def run(args: Sequence[str | Path], *, quiet: bool = True) -> None:
    """Run ffmpeg with the given arguments (without the leading 'ffmpeg')."""
    cmd = [ffmpeg_bin(), "-hide_banner", "-y", *(["-loglevel", "error"] if quiet else []), *map(str, args)]
    log.debug(" ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise FFmpegError(f"ffmpeg failed ({proc.returncode}): {' '.join(cmd)}\n{proc.stderr[-4000:]}")


@dataclass(frozen=True)
class Probe:
    path: Path
    kind: str  # "video" | "image"
    width: int
    height: int
    duration_s: float
    fps: float
    codec: str
    creation_time: str | None
    rotation: int
    raw: dict


def probe(path: Path) -> Probe:
    cmd = [
        ffprobe_bin(),
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise FFmpegError(f"ffprobe failed on {path}: {proc.stderr.strip()}")
    data = json.loads(proc.stdout or "{}")
    streams = [s for s in data.get("streams", []) if s.get("codec_type") == "video"]
    if not streams:
        raise FFmpegError(f"no video/image stream in {path}")
    vs = streams[0]
    fmt = data.get("format", {})
    duration = float(vs.get("duration") or fmt.get("duration") or 0.0)
    num, _, den = (vs.get("avg_frame_rate") or vs.get("r_frame_rate") or "0/1").partition("/")
    fps = float(num) / float(den) if den and float(den) else 0.0
    nb_frames = int(vs.get("nb_frames") or 0)
    is_image = fmt.get("format_name", "").endswith("_pipe") or (
        vs.get("codec_name")
        in {
            "mjpeg",
            "png",
            "webp",
            "tiff",
            "bmp",
        }
        and (duration == 0.0 or nb_frames <= 1)
    )
    tags = {**fmt.get("tags", {}), **vs.get("tags", {})}
    rotation = 0
    for sd in vs.get("side_data_list", []) or []:
        if "rotation" in sd:
            rotation = int(sd["rotation"])
    if "rotate" in tags:
        rotation = int(tags["rotate"])
    return Probe(
        path=path,
        kind="image" if is_image else "video",
        width=int(vs.get("width") or 0),
        height=int(vs.get("height") or 0),
        duration_s=duration,
        fps=fps,
        codec=str(vs.get("codec_name", "")),
        creation_time=tags.get("creation_time"),
        rotation=rotation,
        raw=data,
    )


def extract_frame(video: Path, t: float, out: Path, *, width: int | None = None, quality: int = 2) -> Path:
    """Write one frame at time t (seconds) to `out` (format from extension)."""
    out.parent.mkdir(parents=True, exist_ok=True)
    vf = [f"scale={width}:-2:flags=lanczos"] if width else []
    args: list[str | Path] = ["-ss", f"{max(t, 0):.3f}", "-i", video, "-frames:v", "1"]
    if vf:
        args += ["-vf", ",".join(vf)]
    if out.suffix.lower() in {".jpg", ".jpeg"}:
        args += ["-q:v", str(quality)]
    run([*args, out])
    return out


def iter_frames(
    video: Path, *, every_s: float | None = None, width: int | None = None
) -> Iterator[tuple[float, np.ndarray]]:
    """Decode frames as BGR uint8 arrays, optionally subsampled to one every `every_s` seconds."""
    info = probe(video)
    w, h = info.width, info.height
    if info.rotation in (90, -90, 270, -270):
        w, h = h, w
    filters = []
    if every_s:
        filters.append(f"fps=1/{every_s}")
    if width and width < w:
        h = int(round(h * width / w / 2) * 2)
        w = width
        filters.append(f"scale={w}:{h}:flags=area")
    cmd = [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-i", str(video)]
    if filters:
        cmd += ["-vf", ",".join(filters)]
    cmd += ["-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
    log.debug(" ".join(cmd))
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdout is not None
    frame_bytes = w * h * 3
    step = every_s if every_s else (1.0 / info.fps if info.fps else 0.0)
    i = 0
    try:
        while True:
            buf = proc.stdout.read(frame_bytes)
            if len(buf) < frame_bytes:
                break
            # fps=1/N emits the first frame at t≈N/2; report the sample's center time.
            t = (i + 0.5) * step if every_s else i * step
            yield t, np.frombuffer(buf, np.uint8).reshape(h, w, 3)
            i += 1
    finally:
        proc.stdout.close()
        proc.kill()
        proc.wait()


class FrameWriter:
    """Pipe BGR frames into ffmpeg to produce an H.264 (or HEVC) MP4.

    with FrameWriter(out, 1920, 1080, fps=30) as w:
        w.write(frame_bgr)
    """

    def __init__(
        self,
        out: Path,
        width: int,
        height: int,
        *,
        fps: float = 30,
        codec: str = "h264",
        crf: int = 20,
        preset: str = "medium",
        extra: Sequence[str] = (),
    ) -> None:
        self.out, self.width, self.height = out, width, height
        out.parent.mkdir(parents=True, exist_ok=True)
        if codec == "hevc":
            venc = ["-c:v", "libx265", "-tag:v", "hvc1", "-x265-params", "log-level=error"]
        else:
            venc = ["-c:v", "libx264", "-profile:v", "high"]
        self.cmd = [
            ffmpeg_bin(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "bgr24",
            "-s",
            f"{width}x{height}",
            "-r",
            str(fps),
            "-i",
            "-",
            *venc,
            "-crf",
            str(crf),
            "-preset",
            preset,
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            *extra,
            str(out),
        ]
        self.proc: subprocess.Popen | None = None

    def __enter__(self) -> FrameWriter:
        log.debug(" ".join(self.cmd))
        self.proc = subprocess.Popen(self.cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        return self

    def write(self, frame: np.ndarray) -> None:
        if frame.shape[:2] != (self.height, self.width):
            raise ValueError(f"frame {frame.shape[:2]} != {(self.height, self.width)}")
        assert self.proc and self.proc.stdin
        self.proc.stdin.write(np.ascontiguousarray(frame, dtype=np.uint8).tobytes())

    def __exit__(self, exc_type, exc, tb) -> None:
        assert self.proc and self.proc.stdin
        self.proc.stdin.close()
        err = self.proc.stderr.read().decode() if self.proc.stderr else ""
        code = self.proc.wait()
        if exc_type is None and code != 0:
            raise FFmpegError(f"ffmpeg encode failed ({code}) for {self.out}:\n{err[-4000:]}")
