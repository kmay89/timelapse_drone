"""Pipeline artifacts passed between stages (see docs/ARCHITECTURE.md).

catalog.json    ingest  → select
selection.json  select  → masters
index.json      masters → build / film
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T", bound="Artifact")


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


class MastersVantage(_M):
    name: str
    width: int
    height: int
    captures: list[MasterCapture] = Field(default_factory=list)


class MastersIndex(Artifact):
    vantages: dict[str, MastersVantage] = Field(default_factory=dict)
