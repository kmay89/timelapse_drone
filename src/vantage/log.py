"""Minimal console logging with step timers. No third-party deps."""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager

_VERBOSE = bool(os.environ.get("VANTAGE_VERBOSE"))
_COLOR = sys.stderr.isatty() and not os.environ.get("NO_COLOR")


def set_verbose(on: bool) -> None:
    global _VERBOSE
    _VERBOSE = on


def _paint(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def _emit(prefix: str, msg: str) -> None:
    print(f"{prefix} {msg}", file=sys.stderr, flush=True)


def info(msg: str) -> None:
    _emit(_paint("2", "·"), msg)


def debug(msg: str) -> None:
    if _VERBOSE:
        _emit(_paint("2", "…"), _paint("2", msg))


def ok(msg: str) -> None:
    _emit(_paint("32", "✓"), msg)


def warn(msg: str) -> None:
    _emit(_paint("33", "!"), msg)


def fail(msg: str) -> None:
    _emit(_paint("31", "✗"), msg)


@contextmanager
def step(name: str) -> Iterator[None]:
    """Log a named pipeline step with its wall time."""
    _emit(_paint("36", "▸"), name)
    start = time.perf_counter()
    try:
        yield
    except Exception:
        fail(f"{name} failed after {time.perf_counter() - start:.1f}s")
        raise
    ok(f"{name} ({time.perf_counter() - start:.1f}s)")
