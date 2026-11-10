"""Shared pytest hooks.

Byte-for-byte checks (films, tracks, plans rendered twice) compare files of megabytes; pytest's own
diff of two such `bytes` runs to tens of thousands of lines and buries the test's name. This prints
the sizes, the first differing byte and both digests instead, so a failure reads in a few lines.
"""

from __future__ import annotations

import hashlib

_LONG = 256  # bytes: shorter values keep pytest's own diff, which is readable at that size


def pytest_assertrepr_compare(op: str, left: object, right: object) -> list[str] | None:
    if op != "==" or not isinstance(left, bytes) or not isinstance(right, bytes):
        return None
    if max(len(left), len(right)) <= _LONG:
        return None
    same = (i for i, (a, b) in enumerate(zip(left, right, strict=False)) if a != b)
    first = next(same, min(len(left), len(right)))
    return [
        f"{len(left)} bytes == {len(right)} bytes: they differ",
        f"first difference at byte {first}",
        f"sha256 {hashlib.sha256(left).hexdigest()[:16]} != {hashlib.sha256(right).hexdigest()[:16]}",
    ]
