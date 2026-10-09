"""Typed configuration for a Vantage project.

A project is a folder with three human-editable YAML files:

    project.yaml      settings: title, location, processing and output options
    story.yaml        editorial: vantages, capture labels, chapters, hotspots
    brand/brand.yaml  the client's brand kit (logos, colors, fonts, voice, credits)

These models are the single source of truth for those formats. Everything the
pipeline writes (work/ artifacts, story.json for the web runtime) is derived
from them, so a project can always be rebuilt from YAML + footage.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}){1,2}$")
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# --------------------------------------------------------------------------- #
# Brand kit
# --------------------------------------------------------------------------- #


class BrandLogos(Model):
    """Paths are relative to the brand.yaml file. SVG strongly preferred."""

    primary: str | None = Field(None, description="Full logo for light backgrounds.")
    on_dark: str | None = Field(None, description="Full logo for dark backgrounds (often white).")
    mark: str | None = Field(None, description="Square symbol / monogram; used for favicons and app icon.")
    alt: str | None = Field(None, description="Accessible name for the logo; defaults to brand name.")


class BrandColors(Model):
    """Semantic color roles. The runtime derives every other tone from these."""

    accent: str = Field("#d4a24c", description="Signature color: progress rail, active states, highlights.")
    accent_2: str | None = Field(None, description="Secondary accent for charts and the 'after' side.")
    ink: str = Field("#14110f", description="Primary text on light surfaces.")
    paper: str = Field("#f4f0e8", description="Light surface (credits, light-mode sections).")
    night: str = Field("#0b0c0b", description="Cinematic dark surface behind imagery.")
    muted: str | None = Field(None, description="Secondary text; derived from ink/paper when omitted.")
    extra: dict[str, str] = Field(default_factory=dict, description="Additional named brand colors.")

    @model_validator(mode="after")
    def _check_hex(self) -> BrandColors:
        values = {k: v for k, v in self.model_dump(exclude={"extra"}).items() if v is not None}
        values.update(self.extra)
        for key, value in values.items():
            if not HEX_RE.match(value):
                raise ValueError(f"color {key!r} must be a hex value like #1a2b3c, got {value!r}")
        return self


class FontSpec(Model):
    family: str
    files: list[str] = Field(
        default_factory=list,
        description="Client-supplied .woff2 files relative to brand.yaml (must be licensed for web embedding).",
    )
    bundled: str | None = Field(
        None, description="Name of an open-licensed font shipped with Vantage (see `vantage fonts`)."
    )
    weight: str = Field(
        "100 900", description="CSS font-weight range for variable fonts, or a single weight."
    )
    style: Literal["normal", "italic"] = "normal"
    fallback: str = "system-ui, -apple-system, 'Helvetica Neue', Arial, sans-serif"
    features: str | None = Field(None, description="CSS font-feature-settings, e.g. \"'ss01' 1\".")


class BrandTypography(Model):
    display: FontSpec = Field(
        default_factory=lambda: FontSpec(
            family="Fraunces", bundled="fraunces", fallback="Georgia, 'Times New Roman', serif"
        )
    )
    text: FontSpec = Field(default_factory=lambda: FontSpec(family="Inter", bundled="inter"))
    numeric: FontSpec | None = Field(None, description="Optional font for dates/odometers; defaults to text.")


class BrandVoice(Model):
    tone: str = "Warm, civic, precise. Let the imagery carry the emotion."
    reading_level: str = "General public"
    avoid: list[str] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)


class Partner(Model):
    name: str
    role: str = Field(..., description="e.g. Owner, Landscape Architect, General Contractor, Aerial Imagery.")
    url: str | None = None
    logo: str | None = Field(None, description="Path relative to brand.yaml.")
    logo_on_dark: str | None = None


class BrandKit(Model):
    name: str
    short_name: str | None = None
    url: str | None = None
    logos: BrandLogos = Field(default_factory=BrandLogos)
    colors: BrandColors = Field(default_factory=BrandColors)
    typography: BrandTypography = Field(default_factory=BrandTypography)
    voice: BrandVoice = Field(default_factory=BrandVoice)
    partners: list[Partner] = Field(default_factory=list, description="Co-brands shown in the credits.")
    credit_line: str | None = Field(None, description="e.g. 'Aerial story produced for <client>'.")
    copyright: str | None = None
    disclaimer: str | None = Field(
        None, description="e.g. 'Renderings are conceptual and subject to change.'"
    )
    theme: Literal["dark", "light"] = "dark"
    grain: bool = Field(True, description="Subtle film grain over imagery.")

    # Set by the loader; not part of the YAML.
    root: Path | None = Field(None, exclude=True)

    def resolve(self, rel: str | None) -> Path | None:
        if rel is None:
            return None
        p = Path(rel)
        return p if p.is_absolute() or self.root is None else (self.root / p)


# --------------------------------------------------------------------------- #
# Project settings
# --------------------------------------------------------------------------- #


class Location(Model):
    name: str
    region: str | None = None
    lat: float | None = None
    lon: float | None = None


class AlignSettings(Model):
    method: Literal["auto", "homography", "affine", "none"] = "auto"
    detector: Literal["sift", "akaze", "orb"] = "sift"
    max_features: int = 8000
    detect_width: int = Field(1600, description="Images are downscaled to this width for feature detection.")
    ecc_refine: bool = True
    min_inliers: int = 60
    output_aspect: float | None = Field(None, description="Crop aspect (w/h). Default: reference aspect.")


class GradeSettings(Model):
    mode: Literal["none", "reinhard", "histogram"] = "reinhard"
    strength: float = Field(0.6, ge=0.0, le=1.0, description="0 keeps each flight's look; 1 fully matches.")


class SelectSettings(Model):
    sample_every_s: float = Field(0.5, gt=0)
    max_candidates_per_source: int = 240
    gps_radius_m: float = Field(60.0, description="Telemetry prefilter radius around the vantage hint.")


class ImageSettings(Model):
    widths: list[int] = Field(default_factory=lambda: [960, 1600, 2560])
    formats: list[Literal["avif", "webp", "jpeg"]] = Field(default_factory=lambda: ["avif", "jpeg"])
    quality: dict[str, int] = Field(default_factory=lambda: {"avif": 52, "webp": 80, "jpeg": 82})
    master_width: int = Field(2560, description="Width of the aligned+graded masters kept in the project.")


class FilmSettings(Model):
    enabled: bool = True
    width: int = 1920
    height: int = 1080
    fps: int = 30
    hold_s: float = Field(1.1, description="Seconds each capture holds.")
    fade_s: float = Field(0.9, description="Crossfade seconds between captures.")
    vertical: bool = Field(True, description="Also render a 1080x1920 cut for phones/social.")
    title_card_s: float = 3.0
    end_card_s: float = 3.0


class OutputSettings(Model):
    base_url: str | None = Field(None, description="Canonical hosted URL, used for share cards.")
    single_file: bool = True
    single_file_max_mb: float = 60.0
    lite_max_mb: float = Field(15.0, description="Budget for the email-friendly lite single file.")
    service_worker: bool = True
    film: FilmSettings = Field(default_factory=FilmSettings)
    images: ImageSettings = Field(default_factory=ImageSettings)


class ProjectConfig(Model):
    slug: str
    title: str
    subtitle: str | None = None
    kicker: str | None = Field(
        None, description="Small line above the title, e.g. 'Aurora, Ohio · 2025–2026'."
    )
    dek: str | None = Field(None, description="One-paragraph standfirst under the title.")
    byline: str | None = None
    lang: str = "en"
    location: Location | None = None
    timezone: str = "America/New_York"
    brand: str = "brand/brand.yaml"
    footage_dir: str = Field("footage", description="Raw footage folder (gitignored). Absolute or relative.")
    draft: bool = Field(False, description="Shows a 'Preview' badge.")
    simulated: bool = Field(False, description="Discloses that imagery is simulated/placeholder.")
    align: AlignSettings = Field(default_factory=AlignSettings)
    grade: GradeSettings = Field(default_factory=GradeSettings)
    select: SelectSettings = Field(default_factory=SelectSettings)
    output: OutputSettings = Field(default_factory=OutputSettings)

    @field_validator("slug")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not SLUG_RE.match(v):
            raise ValueError("slug must be lowercase letters/digits separated by single hyphens")
        return v


# --------------------------------------------------------------------------- #
# Story (editorial)
# --------------------------------------------------------------------------- #

# A capture reference: an ISO date ("2026-09-12"), or "earliest" / "latest",
# or an index like "#3" into the date-sorted captures of a vantage.
CaptureRef = str


class FrameRef(Model):
    """Points at one frame of one source file inside footage_dir."""

    source: str = Field(..., description="Path relative to footage_dir.")
    t: float = Field(0.0, description="Seconds into a video; ignored for stills.")


class VantageHint(Model):
    lat: float | None = None
    lon: float | None = None
    alt_m: float | None = None
    heading_deg: float | None = None
    gimbal_pitch_deg: float | None = None


class Vantage(Model):
    """A repeatable viewpoint. Each visit contributes one aligned frame per vantage."""

    id: str
    name: str
    kind: Literal["drone", "ground", "ortho", "archival", "plan"] = "drone"
    portrait_focus: list[float] | None = Field(
        None, description="[x, y] normalized centre of the crop on portrait phones. Default: image centre."
    )
    reference: FrameRef | None = Field(
        None,
        description="The frame every other visit is aligned to. Default: sharpest frame of latest visit.",
    )
    hint: VantageHint | None = None
    picks: dict[str, FrameRef] = Field(
        default_factory=dict, description="Manual per-date overrides: {'2025-06-14': {source, t}}."
    )


class CaptureNote(Model):
    date: dt.date
    label: str | None = Field(None, description="Display label, e.g. 'June 2025'. Default derived from date.")
    note: str | None = None
    exclude: bool = False


class Hotspot(Model):
    id: str
    x: float = Field(..., ge=0, le=1, description="Normalized x in aligned image space.")
    y: float = Field(..., ge=0, le=1, description="Normalized y in aligned image space.")
    label: str
    body: str | None = None
    from_: CaptureRef | None = Field(None, alias="from", description="First capture where it is shown.")
    to: CaptureRef | None = Field(None, description="Last capture where it is shown.")


class Step(Model):
    """A scrollytelling step: a text card tied to a moment in time."""

    capture: CaptureRef | None = None
    text: str = Field(..., description="Markdown.")
    focus: list[float] | None = Field(
        None, description="Optional [x, y, zoom] camera move in normalized coords (zoom >= 1)."
    )
    split: float | None = Field(
        None, ge=0, le=1, description="compare chapters: curtain position for this step."
    )

    @field_validator("focus")
    @classmethod
    def _focus(cls, v: list[float] | None) -> list[float] | None:
        if v is not None and len(v) != 3:
            raise ValueError("focus must be [x, y, zoom]")
        return v


class _Chapter(Model):
    id: str | None = None
    kicker: str | None = None
    title: str | None = None
    body: str | None = Field(None, description="Markdown. May contain {fact:id} tokens.")
    surface: Literal["night", "paper"] | None = Field(
        None, description="Background world for the chapter. Default: night for imagery, paper for text."
    )


class HeroChapter(_Chapter):
    type: Literal["hero"] = "hero"
    vantage: str | None = None
    capture: CaptureRef = "latest"
    video: FrameRef | None = Field(None, description="Optional looping clip start; uses clip_s seconds.")
    clip_s: float = 8.0


class ScrubChapter(_Chapter):
    """Scroll-scrubbed time-lapse of one vantage across every visit."""

    type: Literal["scrub"] = "scrub"
    vantage: str
    from_: CaptureRef = Field("earliest", alias="from")
    to: CaptureRef = "latest"
    steps: list[Step] = Field(default_factory=list)
    hotspots: list[Hotspot] = Field(default_factory=list)
    scroll_vh: float | None = Field(
        None,
        description="Scroll length of the pinned section in viewport heights. Default: 75 per capture, max 900.",
    )
    hold: float = Field(
        0.55, ge=0, lt=1, description="Share of each capture's scroll segment spent holding still."
    )


class CompareChapter(_Chapter):
    """Curtain / blink comparison between two visits of one vantage."""

    type: Literal["compare"] = "compare"
    mode: Literal["curtain", "blink"] = "curtain"
    vantage: str
    before: CaptureRef = "earliest"
    after: CaptureRef = "latest"
    steps: list[Step] = Field(default_factory=list)
    hotspots: list[Hotspot] = Field(default_factory=list)
    before_label: str | None = None
    after_label: str | None = None


class VideoChapter(_Chapter):
    type: Literal["video"] = "video"
    clip: FrameRef
    clip_s: float = 10.0
    caption: str | None = None
    loop: bool = True


class TextChapter(_Chapter):
    type: Literal["text"] = "text"
    pull_quote: str | None = None
    attribution: str | None = None


class Stat(Model):
    value: str
    unit: str | None = None
    label: str
    source: str | None = None


class StatsChapter(_Chapter):
    type: Literal["stats"] = "stats"
    items: list[Stat]


class TimelineItem(Model):
    date: str = Field(..., description="Free-form display date, e.g. '1970' or 'Oct 23, 2025'.")
    title: str
    status: Literal["done", "in-progress", "planned"] | None = None
    body: str | None = None
    capture: CaptureRef | None = None
    vantage: str | None = None
    source: str | None = Field(None, description="URL backing the claim.")


class TimelineChapter(_Chapter):
    type: Literal["timeline"] = "timeline"
    items: list[TimelineItem]


class GalleryImage(Model):
    file: str = Field(
        ..., description="Image path relative to the project folder (archival photos, postcards)."
    )
    caption: str | None = None
    date: str | None = Field(None, description="Display date, e.g. 'c. 1930–45'.")
    credit: str | None = None
    alt: str | None = None


class GalleryChapter(_Chapter):
    """A dated filmstrip: every capture of a vantage, and/or standalone images with credits."""

    type: Literal["gallery"] = "gallery"
    vantage: str | None = None
    captures: list[CaptureRef] = Field(
        default_factory=list, description="Default: all captures of the vantage."
    )
    images: list[GalleryImage] = Field(default_factory=list)


class ExploreChapter(_Chapter):
    """Free-play 'time machine': date scrubber, blink, side-by-side, zoom."""

    type: Literal["explore"] = "explore"
    vantages: list[str] = Field(default_factory=list, description="Default: all vantages.")


class Source(Model):
    label: str
    url: str | None = None


class CreditsChapter(_Chapter):
    type: Literal["credits"] = "credits"
    sources: list[Source] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


Chapter = Annotated[
    HeroChapter
    | ScrubChapter
    | CompareChapter
    | VideoChapter
    | TextChapter
    | StatsChapter
    | TimelineChapter
    | GalleryChapter
    | ExploreChapter
    | CreditsChapter,
    Field(discriminator="type"),
]


class Story(Model):
    vantages: list[Vantage]
    captures: list[CaptureNote] = Field(default_factory=list)
    chapters: list[Chapter]

    @model_validator(mode="after")
    def _refs(self) -> Story:
        ids = [v.id for v in self.vantages]
        if len(ids) != len(set(ids)):
            raise ValueError("vantage ids must be unique")
        known = set(ids)
        for ch in self.chapters:
            vids: list[str] = []
            if isinstance(ch, ScrubChapter | CompareChapter | HeroChapter | GalleryChapter) and ch.vantage:
                vids = [ch.vantage]
            elif isinstance(ch, ExploreChapter):
                vids = ch.vantages
            for vid in vids:
                if vid not in known:
                    raise ValueError(f"chapter {ch.id or ch.type!r} references unknown vantage {vid!r}")
        return self


# --------------------------------------------------------------------------- #
# Facts (every number and claim in the story, with sources and a status)
# --------------------------------------------------------------------------- #

FactStatus = Literal["verified", "reported", "needs-client", "client-approved", "do-not-print"]
FACT_TOKEN_RE = re.compile(r"\{fact:([a-z0-9][a-z0-9_-]*)\}")


class Fact(Model):
    text: str = Field(..., description="What the story prints, e.g. 'Oct. 23, 2025'.")
    sources: list[str] = Field(default_factory=list)
    status: FactStatus = "needs-client"
    attribution: str | None = Field(None, description="Required for 'reported' facts in release builds.")
    note: str | None = None

    @property
    def releasable(self) -> bool:
        if self.status in ("verified", "client-approved"):
            return True
        return self.status == "reported" and bool(self.attribution)


class Facts(Model):
    facts: dict[str, Fact] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #


class Project(BaseModel):
    """A loaded project: config + story + brand, with resolved paths."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    root: Path
    config: ProjectConfig
    story: Story
    brand: BrandKit
    facts: Facts = Facts()

    @property
    def slug(self) -> str:
        return self.config.slug

    @property
    def footage_dir(self) -> Path:
        p = Path(self.config.footage_dir).expanduser()
        return p if p.is_absolute() else self.root / p

    @property
    def masters_dir(self) -> Path:
        """Aligned + graded masters. Small enough to commit; all a build needs."""
        return self.root / "masters"

    @property
    def work_dir(self) -> Path:
        """Heavy intermediates (candidate frames, contact sheets). Never committed."""
        return self.root / "work"


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"missing {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return data


def load_brand(path: Path) -> BrandKit:
    brand = BrandKit.model_validate(_read_yaml(path))
    brand.root = path.parent
    return brand


def load_project(root: Path) -> Project:
    root = root.resolve()
    config = ProjectConfig.model_validate(_read_yaml(root / "project.yaml"))
    story = Story.model_validate(_read_yaml(root / "story.yaml"))
    brand = load_brand(root / config.brand)
    facts_path = root / "facts.yaml"
    facts = Facts.model_validate(_read_yaml(facts_path)) if facts_path.exists() else Facts()
    return Project(root=root, config=config, story=story, brand=brand, facts=facts)
