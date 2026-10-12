"""Pipeline artifacts passed between stages (see docs/ARCHITECTURE.md).

catalog.json    ingest  → select
selection.json  select  → masters
index.json      masters → build / film
<film>.plan.json  film plan → render / package (the FilmPlan of one film, see "Films")
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

T = TypeVar("T", bound="Artifact")
_REL_FILE = re.compile(r"[\w.-]+(?:/[\w.-]+)*")  # matched in full: relative, POSIX separators


class Artifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = 1

    @classmethod
    def load(cls: type[T], path: Path) -> T:
        return cls.model_validate(json.loads(path.read_text(encoding="utf-8")))

    def save(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.model_dump_json(indent=2, exclude_none=True) + "\n", encoding="utf-8")
        return path


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ------------------------------- catalog ---------------------------------- #

DateSource = Literal["manual", "folder", "srt", "exif", "quicktime", "filename", "mtime"]


class Source(_M):
    id: str = Field(..., description="sha1(rel path + size)[:8]")
    path: str = Field(..., description="Relative to footage_dir, POSIX separators.")
    kind: Literal["video", "photo"]
    date: str = Field(..., description="Local flight date YYYY-MM-DD.")
    date_source: DateSource
    start: str | None = Field(None, description="Local ISO datetime of first frame / shutter.")
    duration_s: float = 0.0
    width: int
    height: int
    fps: float = 0.0
    telemetry: str | None = Field(None, description="Sidecar .SRT path relative to footage_dir.")
    lat: float | None = None
    lon: float | None = None
    rel_alt: float | None = None
    heading_deg: float | None = None
    gimbal_pitch_deg: float | None = None
    size_bytes: int = 0


class Candidate(_M):
    source: str = Field(..., description="Source id.")
    t: float = 0.0
    file: str = Field(..., description="JPEG relative to work/, ≤1600 px wide.")
    sharpness: float
    lat: float | None = None
    lon: float | None = None
    rel_alt: float | None = None
    heading_deg: float | None = None
    gimbal_pitch_deg: float | None = None


class Catalog(Artifact):
    sources: list[Source] = Field(default_factory=list)
    candidates: list[Candidate] = Field(default_factory=list)

    def source(self, source_id: str) -> Source:
        for s in self.sources:
            if s.id == source_id:
                return s
        raise KeyError(source_id)

    def source_by_path(self, rel_path: str) -> Source:
        for s in self.sources:
            if s.path == rel_path:
                return s
        raise KeyError(rel_path)

    def dates(self) -> list[str]:
        return sorted({s.date for s in self.sources})


# ------------------------------ selection --------------------------------- #


class FramePick(_M):
    source: str = Field(..., description="Source id.")
    t: float = 0.0
    date: str
    score: float = 0.0
    inliers: int = 0
    manual: bool = False


class VantageSelection(_M):
    reference: FramePick
    picks: dict[str, FramePick] = Field(default_factory=dict, description="date → pick")


class Selection(Artifact):
    vantages: dict[str, VantageSelection] = Field(default_factory=dict)


# ------------------------------- masters ---------------------------------- #


class AlignInfo(_M):
    method: Literal["reference", "homography", "affine", "none"]
    inliers: int = 0
    inlier_ratio: float = 0.0
    rmse_px: float = 0.0
    ecc: float | None = None
    ok: bool = True
    note: str | None = None


class MasterCapture(_M):
    date: str
    file: str = Field(..., description="Relative to masters/.")
    source: str
    t: float = 0.0
    align: AlignInfo

    # build, film and caption open masters/<file> and publish it, so it must stay inside masters/.
    @field_validator("file")
    @classmethod
    def _inside_masters(cls, value: str) -> str:
        if not _REL_FILE.fullmatch(value) or any(p in (".", "..") for p in value.split("/")):
            raise ValueError(f"file must be a relative path inside masters/, got {value!r}")
        return value


class MastersVantage(_M):
    name: str
    width: int
    height: int
    captures: list[MasterCapture] = Field(default_factory=list)


class MastersIndex(Artifact):
    vantages: dict[str, MastersVantage] = Field(default_factory=dict)


# -------------------------------- films ----------------------------------- #

# Shot kinds a plan may hold. ortho, heatmap and insert are reserved for photogrammetry products
# (orthophoto series, DSM-difference layers, committed 3D-reconstruction clips) and refused for now.
FILM_SHOT_KINDS = frozenset({"card", "section", "title", "capture", "curtain", "stat", "end", "rise", "dip"})
RESERVED_SHOT_KINDS = frozenset({"ortho", "heatmap", "insert"})
_PLAN_KEYS = ("slug", "cut", "renderer", "width", "height", "fps", "frames", "inputs", "shots")


def canonical_json(obj: Any) -> bytes:
    """The bytes a plan hash is taken over: sorted keys, no whitespace, UTF-8, no NaN. Floats keep
    Python's shortest round-trip repr, so a plan read back from disk hashes (and renders) the same."""
    return json.dumps(
        obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode()


class FilmPlan(_M):
    """One film: `plan` decides every frame and is hashed; the rest is a record around it.

    `facts` (a status snapshot), `warnings`, `checks` (the ink box of every typeset layer against the
    safe zone), `encode` (bytes, not frames) and `render` (what the encode produced) are not part of
    the hash. The hash is written into plan.json, and `load`
    refuses a file whose plan no longer matches it (edited by hand).
    """

    version: int = 1
    id: str
    plan: dict[str, Any]
    facts: dict[str, dict[str, Any]] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    checks: dict[str, Any] = Field(default_factory=dict)
    encode: dict[str, Any] = Field(default_factory=dict)
    render: dict[str, Any] | None = None

    @field_validator("plan")
    @classmethod
    def _shape(cls, plan: dict[str, Any]) -> dict[str, Any]:
        missing = [k for k in _PLAN_KEYS if k not in plan]
        if missing:
            raise ValueError(f"plan lacks {', '.join(missing)}")
        for shot in plan["shots"]:
            kind = shot.get("kind")
            if kind in RESERVED_SHOT_KINDS:
                raise ValueError(f"shot {shot.get('id')}: kind {kind!r} is reserved and not supported yet")
            if kind not in FILM_SHOT_KINDS:
                raise ValueError(f"shot {shot.get('id')}: unknown kind {kind!r}")
        canonical_json(plan)  # JSON-safe, no NaN or infinity
        return plan

    @property
    def hash(self) -> str:
        """sha256 hex of canonical_json(plan); people quote its first 12 characters."""
        return hashlib.sha256(canonical_json(self.plan)).hexdigest()

    def document(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "id": self.id,
            "hash": self.hash,
            "plan": self.plan,
            "facts": self.facts,
            "warnings": self.warnings,
            "checks": self.checks,
            "encode": self.encode,
            "render": self.render,
        }

    def dumps(self) -> str:
        return json.dumps(self.document(), indent=2, ensure_ascii=False, allow_nan=False) + "\n"

    @classmethod
    def load(cls, path: Path) -> FilmPlan:
        data = json.loads(path.read_text(encoding="utf-8"))
        stored = data.pop("hash", None) if isinstance(data, dict) else None
        plan = cls.model_validate(data)
        if stored != plan.hash:
            raise ValueError(f"{path.name}: its plan does not match its hash (edited by hand?)")
        return plan

    def save(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.dumps(), encoding="utf-8")
        return path
