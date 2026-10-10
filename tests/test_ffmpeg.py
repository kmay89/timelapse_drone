"""ffmpeg subprocess plumbing: a chatty or failing ffmpeg must never hang the pipeline.

A damaged clip makes ffmpeg log every bad macroblock, even at `-loglevel error`: hundreds of KB on
stderr. A stderr pipe that nobody reads fills at about 64 KB and blocks ffmpeg mid-decode, and the
reader of its stdout then waits forever. These tests run the consumer on a watchdog thread, so a
deadlock fails the test instead of hanging the suite.
"""

from __future__ import annotations

import os
import random
import stat
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

import numpy as np
import pytest

from vantage.media import ffmpeg

T = TypeVar("T")

W, H = 64, 48

# Stands in for ffmpeg: logs ~1.3 MB to stderr before touching stdin/stdout, like a damaged clip does.
FAKE_FFMPEG = """\
#!{python}
import os, signal, sys
signal.alarm(30)  # never outlive a deadlocked test
sys.stderr.write("[h264 @ 0x0] error while decoding MB 59 10, bytestream -7\\n" * 24000)
sys.stderr.write("fake ffmpeg: Invalid data found when processing input\\n")
sys.stderr.flush()
if sys.argv[-1] == "-":  # iter_frames: decode to raw frames on stdout
    sys.stdout.buffer.write(bytes({frame_bytes}) * int(os.environ.get("FAKE_FRAMES", "0")))
    sys.stdout.flush()
elif not os.environ.get("FAKE_SKIP_STDIN"):  # FrameWriter: swallow raw frames from stdin
    sys.stdin.buffer.read()
sys.exit(int(os.environ.get("FAKE_EXIT", "0")))
"""


def _within(fn: Callable[[], T], timeout: float = 60.0) -> T:
    """fn() on a daemon thread; fail (rather than hang) if it is still blocked after `timeout` s."""
    result: list[T] = []
    error: list[BaseException] = []

    def target() -> None:
        try:
            result.append(fn())
        except BaseException as exc:
            error.append(exc)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout)
    assert not thread.is_alive(), f"still blocked after {timeout:g} s: ffmpeg deadlocked on a full pipe"
    if error:
        raise error[0]
    return result[0]


def _times(video: Path, **kw) -> list[float]:
    return [t for t, _ in ffmpeg.iter_frames(video, **kw)]


@pytest.fixture
def fake_ffmpeg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    script = tmp_path / "fake-ffmpeg"
    script.write_text(FAKE_FFMPEG.format(python=sys.executable, frame_bytes=W * H * 3), encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(ffmpeg, "ffmpeg_bin", lambda: str(script))
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"")
    probe = ffmpeg.Probe(clip, "video", W, H, 4.0, 10.0, "h264", None, 0, {})
    monkeypatch.setattr(ffmpeg, "probe", lambda _path: probe)
    return clip


def test_iter_frames_survives_damaged_clip(tmp_path: Path) -> None:
    """A real clip with bytes flipped inside mdat decodes to the end instead of hanging."""
    clean = tmp_path / "clean.mp4"
    source = ["-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30:duration=40"]
    encode = ["-c:v", "libx264", "-preset", "ultrafast", "-g", "30", "-pix_fmt", "yuv420p"]
    ffmpeg.run([*source, *encode, "-movflags", "+faststart", clean])
    data = bytearray(clean.read_bytes())
    start = data.find(b"mdat") + 1024  # faststart: moov first, then mdat runs to the end of the file
    rng = random.Random(1)
    for _ in range(15000):
        data[rng.randrange(start, len(data))] = rng.randrange(256)
    damaged = tmp_path / "damaged.mp4"
    damaged.write_bytes(bytes(data))

    times = _within(lambda: _times(damaged, every_s=0.5, width=320))
    assert len(times) == pytest.approx(80, abs=4)  # one sample every 0.5 s over 40 s


def test_iter_frames_drains_stderr(fake_ffmpeg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_FRAMES", "40")
    times = _within(lambda: _times(fake_ffmpeg))
    assert times == pytest.approx([i / 10 for i in range(40)])


def test_iter_frames_raises_when_ffmpeg_fails(fake_ffmpeg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_EXIT", "1")
    with pytest.raises(ffmpeg.FFmpegError, match=r"(?s)decode failed \(1\).*Invalid data found"):
        _within(lambda: _times(fake_ffmpeg))


def test_iter_frames_stopped_early_is_not_an_error(
    fake_ffmpeg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A consumer that stops early (sample_video's max_frames) kills ffmpeg; that is not a failure."""
    monkeypatch.setenv("FAKE_FRAMES", "40")
    monkeypatch.setenv("FAKE_EXIT", "1")

    def first_two() -> list[float]:
        frames = ffmpeg.iter_frames(fake_ffmpeg)
        times = [next(frames)[0], next(frames)[0]]
        frames.close()
        return times

    assert _within(first_two) == [0.0, 0.1]


def test_frame_writer_drains_stderr(fake_ffmpeg: Path, tmp_path: Path) -> None:
    def encode() -> None:
        with ffmpeg.FrameWriter(tmp_path / "out.mp4", W, H, fps=10) as writer:
            for i in range(40):  # ~370 KB of frames: far more than stdin's pipe holds
                writer.write(np.full((H, W, 3), i, np.uint8))

    _within(encode)


def test_frame_writer_reports_stderr_tail(
    fake_ffmpeg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_EXIT", "1")

    def encode() -> None:
        with ffmpeg.FrameWriter(tmp_path / "out.mp4", W, H, fps=10) as writer:
            writer.write(np.zeros((H, W, 3), np.uint8))

    with pytest.raises(ffmpeg.FFmpegError, match=r"(?s)encode failed \(1\).*Invalid data found") as err:
        _within(encode)
    assert len(str(err.value)) < 4200  # only the tail of a megabyte of log
    assert os.fspath(tmp_path / "out.mp4") in str(err.value)


def test_frame_writer_reports_why_ffmpeg_quit_mid_encode(tmp_path: Path) -> None:
    """ffmpeg that quits while frames are still coming (bad filter, full disk) is reported with its
    own reason, not as the '[Errno 32] Broken pipe' the next write hits."""

    def encode() -> None:
        out = tmp_path / "out.mp4"
        with ffmpeg.FrameWriter(out, W, H, fps=10, preset="ultrafast", extra=["-vf", "no_such_filter"]) as w:
            for _ in range(400):  # ~3.7 MB of frames: far more than stdin's pipe holds
                w.write(np.zeros((H, W, 3), np.uint8))

    with pytest.raises(ffmpeg.FFmpegError, match=r"(?s)encode failed \(\d+\) for .*out\.mp4.*no_such_filter"):
        _within(encode)


def test_frame_writer_reports_why_ffmpeg_quit_before_flush(
    fake_ffmpeg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A small last frame still buffered in stdin is flushed on close into an ffmpeg that already
    quit; that is reported with ffmpeg's stderr too, not as a broken pipe."""
    monkeypatch.setenv("FAKE_EXIT", "1")
    monkeypatch.setenv("FAKE_SKIP_STDIN", "1")  # rejects its input without reading it

    def encode() -> None:
        with ffmpeg.FrameWriter(tmp_path / "out.mp4", 8, 8, fps=10) as writer:
            writer.write(np.zeros((8, 8, 3), np.uint8))  # 192 bytes: stays in the stdin buffer
            assert writer.proc is not None
            writer.proc.wait()

    with pytest.raises(ffmpeg.FFmpegError, match=r"(?s)encode failed \(1\).*Invalid data found"):
        _within(encode)
