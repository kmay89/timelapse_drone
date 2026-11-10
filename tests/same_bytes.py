"""Byte-identity assertions for render outputs, with a short failure message.

`assert a.read_bytes() == b.read_bytes()` on two MP4s prints a byte diff tens of thousands of lines
long when it fails, and CI logs keep only the tail, which loses the test's name. `assert_same_bytes`
compares SHA-256 digests and, on a mismatch, says which file, both sizes and digests, and the first
differing offset with a few bytes of context and the top-level MP4 box it is in; for two MP4s, also
whether the decoded frames differ and from which frame (a render that varied) or only the container.
"""

from __future__ import annotations

import hashlib
import struct
import subprocess
from pathlib import Path

CONTEXT = 16  # bytes shown either side of the first difference


def _boxes(data: bytes) -> list[tuple[int, int, str]]:
    """Top-level (offset, size, type) of an MP4, or [] for anything that does not parse as one."""
    out, i = [], 0
    while i + 8 <= len(data):
        size, kind = struct.unpack(">I4s", data[i : i + 8])
        if size == 1 and i + 16 <= len(data):
            size = struct.unpack(">Q", data[i + 8 : i + 16])[0]
        elif size == 0:
            size = len(data) - i
        if size < 8 or not kind.isascii() or not kind.decode("ascii").isprintable():
            return []
        out.append((i, size, kind.decode("ascii")))
        i += size
    return out if i == len(data) else []


def _where(data: bytes, offset: int) -> str:
    for start, size, kind in _boxes(data):
        if start <= offset < start + size:
            return f" (in '{kind}' at {start}, {size} bytes)"
    return ""


def describe_difference(a: bytes, b: bytes, name: str = "") -> str:
    """One paragraph saying how `a` and `b` differ (empty when they are identical)."""
    if a == b:
        return ""
    at = next((i for i, (x, y) in enumerate(zip(a, b, strict=False)) if x != y), min(len(a), len(b)))
    lo = max(0, at - CONTEXT)
    head = f"{name}: " if name else ""
    return (
        f"{head}{len(a)} bytes sha256 {hashlib.sha256(a).hexdigest()[:16]} != "
        f"{len(b)} bytes sha256 {hashlib.sha256(b).hexdigest()[:16]}; "
        f"first difference at byte {at}{_where(a, at)}:\n"
        f"  a[{lo}:] {a[lo : at + CONTEXT].hex(' ')}\n"
        f"  b[{lo}:] {b[lo : at + CONTEXT].hex(' ')}"
    )


def _ffmpeg(*args: str | Path) -> bytes:
    from vantage.media.ffmpeg import ffmpeg_bin

    run = subprocess.run([ffmpeg_bin(), "-v", "error", *map(str, args)], capture_output=True)
    return run.stdout if run.returncode == 0 else b""


def describe_video(a: Path, b: Path) -> str:
    """For two MP4s that differ: whether their H.264 streams differ, and the first decoded frame
    that does (a render that varied, not the container), from ffmpeg's framemd5."""

    def frames(path: Path) -> list[str]:
        out = _ffmpeg("-i", path, "-map", "0:v:0", "-f", "framemd5", "-").decode()
        return [line.rsplit(",", 1)[-1].strip() for line in out.splitlines() if line and line[0] != "#"]

    def stream(path: Path) -> str:
        return hashlib.sha256(
            _ffmpeg("-i", path, "-map", "0:v:0", "-c", "copy", "-f", "h264", "-")
        ).hexdigest()

    fa, fb = frames(a), frames(b)
    if not fa or not fb:
        return ""
    if fa == fb:
        same = "H.264 streams identical" if stream(a) == stream(b) else "H.264 streams differ"
        return f"\n  {same}; all {len(fa)} decoded frames identical (the container differs)"
    diff = [k for k, (x, y) in enumerate(zip(fa, fb, strict=False)) if x != y]
    first = diff[0] if diff else min(len(fa), len(fb))
    return (
        f"\n  decoded frames differ: {len(diff)} of {len(fa)} vs {len(fb)} frames, "
        f"first at frame {first}, last at frame {diff[-1] if diff else first}"
    )


def assert_same_bytes(a: Path, b: Path, name: str | None = None) -> None:
    """Assert two files hold the same bytes, failing with a few lines instead of a byte diff."""
    da, db = a.read_bytes(), b.read_bytes()
    if hashlib.sha256(da).digest() != hashlib.sha256(db).digest():
        video = describe_video(a, b) if a.suffix.lower() == ".mp4" else ""
        raise AssertionError(describe_difference(da, db, name or a.name) + video)
