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
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}){1,2}$")
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
# Matched in full. A vantage id names folders (masters/<id>/, work/review/<id>/) and a pick date
# names a file (<date>.jpg) that `vantage process` writes and clears.
VANTAGE_ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")
ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

# CSS values a FontSpec may carry (matched in full): font names are identifiers or quoted strings
# without quotes, backslashes, ; { } ( ) < > or control characters; weights and feature tags are
# the plain forms of font-weight and font-feature-settings.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_IDENT = r"-?[^\W\d][\w-]*"
_NAME = rf"""(?:'[^'"\\;{{}}()<>\x00-\x1f\x7f]*'|"[^'"\\;{{}}()<>\x00-\x1f\x7f]*"|{_IDENT}(?: +{_IDENT})*)"""
_WEIGHT = r"(?:normal|bold|\d{1,4})"
_FEATURE = r"""(?:'[A-Za-z0-9]{4}'|"[A-Za-z0-9]{4}")(?: +(?:\d{1,3}|on|off))?"""
_FONT_CSS = {
    "fallback": (
        re.compile(rf"{_NAME}(?: *, *{_NAME})*"),
        "a comma-separated list of font names, e.g. \"Georgia, 'Times New Roman', serif\"",
    ),
    "weight": (re.compile(rf"{_WEIGHT}(?: +{_WEIGHT})?"), 'a font weight or range, e.g. "400" or "100 900"'),
    "features": (
        re.compile(rf"normal|{_FEATURE}(?: *, *{_FEATURE})*"),
        "CSS font-feature-settings, e.g. \"'tnum' 1, 'lnum' 1\"",
    ),
}


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

    # These fields are written into the theme CSS of every edition, so each must be a value of its
    # own kind: never a way to close the declaration and add rules, url()s or other properties.
    @field_validator("family")
    @classmethod
    def _plain_family(cls, value: str) -> str:
        if _CONTROL_RE.search(value):
            raise ValueError(f"family must be plain text, without newlines or control characters: {value!r}")
        return value

    @field_validator("fallback", "weight", "features")
    @classmethod
    def _css_value(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None:
            return None
        pattern, hint = _FONT_CSS[info.field_name or ""]
        value = value.strip(" ")
        if not pattern.fullmatch(value):
            raise ValueError(f"{info.field_name} must be {hint} (it goes into the page's CSS), got {value!r}")
        return value


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
    """Where the site is. Stories publish name and region only; lat/lon never leave project.yaml."""

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
    require_approval: bool = Field(
        False,
        description="package --release needs each film's plan hash under approvals[].films in approvals.yaml.",
    )
    cut: Literal["story", "classic"] = Field(
        "story", description="The cut of the derived films: story (on a beat grid) or classic (hold/fade)."
    )
    bpm: float = Field(100.0, ge=60, le=160, description="The beat grid of story cuts.")
    max_upscale: float = Field(
        1.6, ge=1.0, le=3.0, description="Output px per master px never exceeds this in a push."
    )
    web: bool = Field(True, description="Also write <name>-web.mp4, a copy of at most 25 MiB for websites.")
    tracks: bool = Field(
        True,
        description="Captions (.vtt, .srt), chapters and a timed transcript (.txt) per film, also embedded in the MP4.",
    )


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
    kicker: str | None = Field(None, description="Small line above the title, e.g. 'Riverside · 2025–2026'.")
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


def _as_text(value: Any) -> Any:
    """YAML reads an unquoted 2025-06-14 as a date and 1970 as an int; keep the text the writer typed."""
    if isinstance(value, dt.date) or (isinstance(value, int) and not isinstance(value, bool)):
        return str(value)
    return value


# Display text that is often a bare year or date ('1970', 2025-10-23), quoted or not in the YAML.
DisplayDate = Annotated[str, BeforeValidator(_as_text)]
# A capture reference: an ISO date ("2026-09-12", quoted or not), or "earliest" / "latest",
# or an index like "#3" into the date-sorted captures of a vantage.
CaptureRef = Annotated[str, BeforeValidator(_as_text)]


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

    # Both end up in paths `vantage process` writes and deletes, so neither may lead out of the project.
    @field_validator("id")
    @classmethod
    def _plain_id(cls, value: str) -> str:
        if not VANTAGE_ID_RE.fullmatch(value):
            raise ValueError(
                "vantage id must be lowercase letters, digits, '-' and '_', starting with a letter or "
                f"digit (it names folders under masters/ and work/review/), got {value!r}"
            )
        return value

    @field_validator("picks", mode="before")
    @classmethod
    def _date_keys(cls, value: Any) -> Any:
        """Keys are flight dates, normalized to 'YYYY-MM-DD' (quoted or not in the YAML)."""
        if not isinstance(value, dict):
            return value
        out: dict[str, Any] = {}
        for key, ref in value.items():
            if isinstance(key, dt.date) and not isinstance(key, dt.datetime):
                date = key.isoformat()
            elif isinstance(key, str) and ISO_DATE_RE.fullmatch(key):
                try:
                    date = dt.date.fromisoformat(key).isoformat()
                except ValueError:
                    raise ValueError(f"pick key {key!r} is not a real date") from None
            else:
                raise ValueError(f"pick key {key!r} must be a flight date, YYYY-MM-DD")
            if date in out:
                raise ValueError(f"two picks for {date}")
            out[date] = ref
        return out


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


def _check_focus(v: list[float] | None) -> list[float] | None:
    if v is not None and len(v) != 3:
        raise ValueError("focus must be [x, y, zoom]")
    return v


class StepFilm(Model):
    """How a step reads in the films: the words on screen and the camera target."""

    line: str | None = Field(
        None, description="Words on screen in films (default: the step's leading **bold** run)."
    )
    focus: list[float] | None = Field(
        None,
        description="[x, y, zoom] push target in films (default: the step's focus, kept on its subjects).",
    )
    skip: bool = Field(
        False, description="Leave the step out of films; its capture still plays as a quiet beat."
    )

    @field_validator("focus")
    @classmethod
    def _focus(cls, v: list[float] | None) -> list[float] | None:
        return _check_focus(v)


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
    film: StepFilm | None = Field(None, description="Films only: words on screen, push target, skip.")

    @field_validator("focus")
    @classmethod
    def _focus(cls, v: list[float] | None) -> list[float] | None:
        return _check_focus(v)


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
    date: DisplayDate = Field(..., description="Free-form display date, e.g. '1970' or 'Oct 23, 2025'.")
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
    date: DisplayDate | None = Field(None, description="Display date, e.g. 'c. 1930–45'.")
    credit: str | None = None
    alt: str | None = None


class GalleryFilm(Model):
    """How a gallery chapter reads in story films: a short montage of its vantage (film/story.py)."""

    line: str | None = Field(
        None,
        description="Words over the montage's first image (default: the chapter's kicker, else its title).",
    )
    skip: bool = Field(False, description="Leave the chapter's montage out of films.")


class GalleryChapter(_Chapter):
    """A dated filmstrip: every capture of a vantage, and/or standalone images with credits."""

    type: Literal["gallery"] = "gallery"
    vantage: str | None = None
    captures: list[CaptureRef] = Field(
        default_factory=list, description="Default: all captures of the vantage."
    )
    images: list[GalleryImage] = Field(default_factory=list)
    film: GalleryFilm | None = Field(
        None,
        description="Story films: the montage's words, or skip (a drone vantage other than the main one).",
    )


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


# Matched in full; a film id names files (<slug>-<id>.mp4, …) and never ends in "-web".
FILM_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")


class FilmSpec(Model):
    """One film of story.yaml `films:`. Every field is optional; precedence is
    FilmSpec field > preset > output.film > built-in default (see docs/ARCHITECTURE.md "Films")."""

    preset: Literal["screen"] | None = Field(None, description="screen: the silent event-screen loop.")
    cut: Literal["story", "classic"] | None = None
    aspect: Literal["16x9", "9x16"] | None = None
    bpm: float | None = Field(None, ge=60, le=160)
    chapters: Literal["all", "main"] | list[str] | None = Field(
        None,
        description="all | main (the first scrub's vantage, plus stats and other vantages' gallery montages) "
        "| [chapter ids].",
    )
    stats: int | None = Field(None, ge=0, le=6, description="Stats shown (default 3).")
    montage: int | None = Field(
        None,
        ge=0,
        le=8,
        description="Captures in each gallery chapter's montage (default 5; at least 2; 0: no montages).",
    )
    stats_surface: Literal["night", "paper"] | None = Field(
        None,
        description="Set it to put the film's stats on a plain card of this surface. By default they sit "
        "over the main vantage's latest picture, under a panel tinted by the stats chapter's `surface`.",
    )
    title_beats: int | None = Field(
        None, ge=3, le=16, description="At least 3: the title's words rise on beat 1."
    )
    end_beats: int | None = Field(
        None, ge=2, le=16, description="At least 2: a dip, then the end card's text."
    )
    safe: list[float] | None = Field(None, description="[top, right, bottom, left] safe margins, fractions.")
    type_scale: float | None = Field(None, gt=0.25, le=4)
    max_upscale: float | None = Field(None, ge=1.0, le=3.0)
    loop: bool | None = None
    sound: str = Field("none", description='none ("file" is reserved).')
    auto: bool | None = Field(None, description="Rendered by `vantage film` without --film (default true).")
    enabled: bool | None = None
    hours: float | None = Field(None, gt=0, le=12, description="Loop films: --hours default.")

    @field_validator("montage")
    @classmethod
    def _montage(cls, v: int | None) -> int | None:
        if v == 1:
            raise ValueError("montage must be 0 (no montages) or 2–8 captures: a montage spans first to last")
        return v

    @field_validator("safe")
    @classmethod
    def _safe(cls, v: list[float] | None) -> list[float] | None:
        if v is not None and (len(v) != 4 or not all(0 <= x < 0.45 for x in v)):
            raise ValueError("safe must be [top, right, bottom, left], each a fraction in [0, 0.45)")
        return v

    @field_validator("sound")
    @classmethod
    def _sound(cls, v: str) -> str:
        if v != "none":
            raise ValueError(f"sound {v!r} is not supported yet (films are silent: sound: none)")
        return v


class Story(Model):
    vantages: list[Vantage]
    captures: list[CaptureNote] = Field(default_factory=list)
    chapters: list[Chapter]
    films: dict[str, FilmSpec] = Field(
        default_factory=dict, description="Films by id, merged over the derived 16x9 and 9x16."
    )

    @field_validator("films", mode="before")
    @classmethod
    def _film_ids(cls, value: Any) -> Any:
        """Ids name files: [a-z0-9][a-z0-9-]{0,31}, not ending in -web, unique as typed (1080 == "1080")."""
        if not isinstance(value, dict):
            return value
        out: dict[str, Any] = {}
        for key, spec in value.items():
            fid = _as_text(key)
            if not isinstance(fid, str) or not FILM_ID_RE.fullmatch(fid) or fid.endswith("-web"):
                raise ValueError(f"film id {key!r} must match [a-z0-9][a-z0-9-]{{0,31}} and not end in -web")
            if fid in out:
                raise ValueError(f"film id {fid!r} is declared twice")
            out[fid] = {} if spec is None else spec
        return out

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
