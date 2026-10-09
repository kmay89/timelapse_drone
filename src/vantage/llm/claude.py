"""Optional Claude vision helper: draft captions, alt text, milestones and hotspot ideas.

`vantage caption <project>` shows Claude the first capture of every vantage and then
each consecutive pair of masters (downscaled JPEGs), with the story's context and the
pilot's field notes (footage/<date>/notes.txt), and asks for a structured
`PairAnalysis`. The results go to work/llm/suggestions.yaml: one entry per vantage and
date, all `status: proposed`, each with a ready-to-paste story.yaml step and hotspot
ideas from the change boxes. Nothing here is required for a build, and nothing is
accepted automatically: a human copies what they like into story.yaml.

Responses are cached as work/llm/cache/<sha256>.json, keyed on the model, the prompt
version, the request text and the master bytes, so re-runs are free and deterministic
(the model takes no sampling parameters). `anthropic` is an optional extra
(`uv sync --extra llm`); without it, or without credentials, this warns and returns None.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from vantage import log
from vantage.config import Project
from vantage.film.render import capture_labels
from vantage.models import MasterCapture, MastersIndex

DEFAULT_MODEL = "claude-opus-5-5"
PROMPT_VERSION = "pair-v1"
MAX_EDGE = 1568  # long edge sent to the API
JPEG_QUALITY = 85
CAPTION_CHARS = 220
ALT_CHARS = 150
# Server-side refusal fallbacks ("default" routes by refusal category) on the models that take them.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
FALLBACK_MODELS = ("claude-opus-5", "claude-fable-5", "claude-sonnet-5-5")

SYSTEM = """\
You review aligned aerial photographs of one site taken on separate visits, weeks or months \
apart, for an editorial progress story. Describe only what is visible. Never invent numbers, \
dates, names, owners or purposes; hedge when unsure ("appears to"). Visits are spaced out, so \
say a change is "visible by" a visit, never that it happened on that date. Boxes are \
[x0, y0, x1, y1] fractions of the image width and height from the top-left corner. Captions \
are plain, factual sentences in the brand voice you are given."""
JSON_ONLY = "\n\nReply with only a JSON object (no prose, no code fence) matching this JSON schema:\n{schema}"


def _clip(text: str, limit: int) -> str:
    """Shorten at a word boundary with an ellipsis (the schema can't enforce lengths)."""
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rsplit(" ", 1)[0].rstrip(",;:") + "…"


class Change(BaseModel):
    model_config = ConfigDict(extra="forbid")

    element: str = Field(
        description="What changed, in two or three words, e.g. 'boardwalk' or 'parking lot'."
    )
    description: str = Field(description="One factual sentence describing the visible change.")
    box: list[float] | None = Field(
        description="[x0, y0, x1, y1] in 0-1 image fractions around the change in the later image; null if diffuse."
    )

    @field_validator("box")
    @classmethod
    def _box(cls, v: list[float] | None) -> list[float] | None:
        if v is None or len(v) != 4:
            return None
        x0, y0, x1, y1 = (min(max(c, 0.0), 1.0) for c in v)
        return [min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)]


class PairAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    usable: bool = Field(description="False if the later image is unusable (blur, glare, fog, wrong view).")
    issues: list[str] = Field(
        description="Image problems (blur, haze, glare, misalignment, obstruction); empty if none."
    )
    changes: list[Change] = Field(description="Visible changes in the later image, most significant first.")
    milestones: list[str] = Field(
        description="Milestones visible for the first time, phrased 'X visible by <label>'."
    )
    caption: str = Field(
        description=f"At most {CAPTION_CHARS} characters. What changed (or, for a first visit, what is there), "
        "factual, no invented numbers or dates; refer to the visit as 'visible by <label>'."
    )
    alt_text: str = Field(
        description=f"At most {ALT_CHARS} characters. Literal description of the later image."
    )
    alignment_ok: bool = Field(
        description="True if stable features (roads, shoreline, roofs) line up between images."
    )

    @field_validator("caption")
    @classmethod
    def _caption(cls, v: str) -> str:
        return _clip(v, CAPTION_CHARS)

    @field_validator("alt_text")
    @classmethod
    def _alt(cls, v: str) -> str:
        return _clip(v, ALT_CHARS)


@dataclass(frozen=True)
class _Visit:
    date: str
    label: str
    path: Path
    note: str | None  # story.yaml capture note
    field_notes: str | None  # footage/<date>/notes.txt


@dataclass(frozen=True)
class _Answer:
    analysis: PairAnalysis | None
    served_by: str | None = None
    problem: str | None = None  # why there is no analysis
    refused: bool = False


def _image_block(path: Path) -> dict[str, Any]:
    with Image.open(path) as im:
        im = im.convert("RGB")
        im.thumbnail((MAX_EDGE, MAX_EDGE), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=JPEG_QUALITY)
    data = base64.standard_b64encode(buf.getvalue()).decode("ascii")
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}}


def _context(project: Project, name: str, visits: list[_Visit], cur: _Visit, captions: list[str]) -> str:
    cfg, voice = project.config, project.brand.voice
    lines = [f"Project: {cfg.title}" + (f" — {cfg.subtitle}" if cfg.subtitle else "")]
    if cfg.location:
        lines.append(f"Location: {', '.join(filter(None, (cfg.location.name, cfg.location.region)))}")
    lines += [f"View: {name}", f"Visits of this view: {', '.join(v.label for v in visits)}"]
    lines.append(f"Brand voice: {voice.tone}" + (f" Avoid: {', '.join(voice.avoid)}." if voice.avoid else ""))
    if cur.note:
        lines.append(f"Editor's note for {cur.label}: {cur.note}")
    if cur.field_notes:
        lines.append(f"Pilot's field notes for {cur.label}:\n{cur.field_notes}")
    if captions:
        lines.append("Captions already drafted for earlier visits (do not repeat them):")
        lines += [f"- {c}" for c in captions]
    return "\n".join(lines)


def _request(
    project: Project, name: str, visits: list[_Visit], i: int, captions: list[str]
) -> list[str | Path]:
    """The user turn for visit i: text and master images (as paths), interleaved."""
    cur, prev = visits[i], visits[i - 1] if i else None
    context = _context(project, name, visits, cur, captions)
    if prev is None:
        return [
            f"Image 1: {name}, first visit, {cur.label} ({cur.date}).",
            cur.path,
            f"{context}\n\nThis is the first visit: there is no earlier image. Describe the site as it is "
            "(changes: notable features with boxes; milestones: empty), check whether the image is usable, "
            f"and draft the caption for {cur.label} and the alt text.",
        ]
    return [
        f"Image 1: {name}, {prev.label} ({prev.date}).",
        prev.path,
        f"Image 2: the same aligned view, {cur.label} ({cur.date}).",
        cur.path,
        f"{context}\n\nCompare image 2 with image 1. List what changed (boxes in image 2), say whether "
        f"image 2 is usable and whether the two line up, and draft the caption for {cur.label} "
        f'("visible by {cur.label}") and the alt text for image 2.',
    ]


def _cache_key(model: str, parts: list[str | Path]) -> str:
    """Model, prompt version, request text and the masters' bytes (not their re-encoded JPEGs)."""
    items = [p if isinstance(p, str) else hashlib.sha256(p.read_bytes()).hexdigest() for p in parts]
    payload = json.dumps([model, PROMPT_VERSION, SYSTEM, items], ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def _ask(client: Any, model: str, parts: list[str | Path]) -> _Answer:
    """One structured request; refusals and unparseable replies come back as problems, not exceptions."""
    import anthropic

    content = [{"type": "text", "text": p} if isinstance(p, str) else _image_block(p) for p in parts]
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": 16000,
        "system": SYSTEM,
        "messages": [{"role": "user", "content": content}],
    }
    to_schema = getattr(anthropic, "transform_schema", None)
    if to_schema:  # structured outputs: the reply is guaranteed to be schema-valid JSON
        schema = to_schema(PairAnalysis)
        kwargs["output_config"] = {"effort": "medium", "format": {"type": "json_schema", "schema": schema}}
    else:  # older SDK: ask for JSON and validate it ourselves
        kwargs["system"] = SYSTEM + JSON_ONLY.format(schema=json.dumps(PairAnalysis.model_json_schema()))
    if model.startswith(FALLBACK_MODELS):
        kwargs["extra_headers"] = {"anthropic-beta": FALLBACK_BETA}
        kwargs["extra_body"] = {"fallbacks": "default"}
    response = client.messages.create(**kwargs)
    served_by = getattr(response, "model", model)
    if response.stop_reason == "refusal":
        details = getattr(response, "stop_details", None)
        why = getattr(details, "category", None) or getattr(details, "explanation", None) or "no details"
        return _Answer(None, served_by, f"refused ({why})", refused=True)
    text = "".join(block.text for block in response.content if block.type == "text").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return _Answer(PairAnalysis.model_validate_json(text), served_by)
    except ValidationError as exc:
        return _Answer(
            None,
            served_by,
            f"unparseable reply (stop_reason {response.stop_reason}): {exc.errors()[0]['msg']}",
        )


def _client() -> Any | None:
    """An Anthropic client, or None (with a warning) when the SDK or credentials are missing."""
    try:
        import anthropic
    except ImportError:
        log.warn("captions need the optional `anthropic` package: uv sync --extra llm")
        return None
    try:
        client = anthropic.Anthropic()
    except Exception as exc:  # e.g. a broken credentials profile
        log.warn(f"cannot create a Claude client ({exc}); set ANTHROPIC_API_KEY or run `ant auth login`")
        return None
    # The SDK resolves ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN and `ant auth login` profiles.
    if not any(getattr(client, attr, None) for attr in ("api_key", "auth_token", "credentials")):
        log.warn("no Claude credentials: set ANTHROPIC_API_KEY (or run `ant auth login`) to draft captions")
        return None
    return client


def _visits(project: Project, captures: list[MasterCapture]) -> list[_Visit]:
    notes = {n.date.isoformat(): n for n in project.story.captures}
    kept = sorted(
        (c for c in captures if not (c.date in notes and notes[c.date].exclude)), key=lambda c: c.date
    )
    labels = capture_labels(project, [c.date for c in kept])
    visits = []
    for c in kept:
        note = notes.get(c.date)
        field_notes = project.footage_dir / c.date / "notes.txt"
        visits.append(
            _Visit(
                c.date,
                labels[c.date],
                project.masters_dir / c.file,
                note.note if note else None,
                field_notes.read_text(encoding="utf-8").strip() if field_notes.is_file() else None,
            )
        )
    return visits


def _entry(vid: str, visits: list[_Visit], i: int, answer: _Answer) -> dict[str, Any]:
    cur = visits[i]
    entry: dict[str, Any] = {
        "id": f"{vid}-{cur.date}",
        "date": cur.date,
        "label": cur.label,
        "compared_with": visits[i - 1].date if i else None,
        "status": "proposed" if answer.analysis else ("refused" if answer.refused else "failed"),
        "model": answer.served_by,
    }
    if answer.analysis is None:
        return {**entry, "problem": answer.problem}
    a = answer.analysis
    hotspots = []
    for change in a.changes:
        if change.box is None:
            continue
        x0, y0, x1, y1 = change.box
        slug = re.sub(r"[^a-z0-9]+", "-", change.element.lower()).strip("-") or "change"
        hotspots.append(
            {
                "id": f"{slug}-{cur.date}",
                "x": round((x0 + x1) / 2, 3),
                "y": round((y0 + y1) / 2, 3),
                "label": change.element[:1].upper() + change.element[1:],
                "body": change.description,
                "from": cur.date,
            }
        )
    return {
        **entry,
        "analysis": a.model_dump(),
        "step": {"capture": cur.date, "text": a.caption},
        "hotspots": hotspots,
    }


def draft_captions(project: Project) -> Path | None:
    """Draft captions/alt text/hotspots for every visit → work/llm/suggestions.yaml (None if unavailable)."""
    index_path = project.masters_dir / "index.json"
    if not index_path.is_file():
        raise FileNotFoundError(f"no {index_path}; run `vantage process {project.slug}` first")
    client = _client()
    if client is None:
        return None
    import anthropic

    model = os.environ.get("VANTAGE_CLAUDE_MODEL") or DEFAULT_MODEL
    cache_dir = project.work_dir / "llm" / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    index = MastersIndex.load(index_path)
    names = {v.id: v.name for v in project.story.vantages}
    out: dict[str, list[dict[str, Any]]] = {}
    hits = calls = 0
    for vid, mv in index.vantages.items():
        name, visits = names.get(vid, mv.name), _visits(project, mv.captures)
        captions: list[str] = []
        out[vid] = []
        for i in range(len(visits)):
            parts = _request(project, name, visits, i, captions)
            cached = cache_dir / f"{_cache_key(model, parts)}.json"
            if cached.is_file():
                data = json.loads(cached.read_text(encoding="utf-8"))
                answer = _Answer(PairAnalysis.model_validate(data["analysis"]), data["served_by"])
                hits += 1
            else:
                try:
                    answer = _ask(client, model, parts)
                except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
                    log.warn(f"Claude rejected the credentials ({exc}); no captions drafted")
                    return None
                except anthropic.APIError as exc:
                    raise ConnectionError(
                        f"Claude request failed for {vid} {visits[i].date}: {exc} "
                        "(finished visits are cached; re-run to resume)"
                    ) from exc
                calls += 1
                if answer.analysis:
                    record = {
                        "model": model,
                        "served_by": answer.served_by,
                        "analysis": answer.analysis.model_dump(),
                    }
                    cached.write_text(
                        json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
                    )
                else:
                    log.warn(f"{vid} {visits[i].date}: {answer.problem}")
            if answer.analysis:
                captions.append(answer.analysis.caption)
            out[vid].append(_entry(vid, visits, i, answer))
    path = project.work_dir / "llm" / "suggestions.yaml"
    header = (
        "# Draft captions, alt text and hotspot ideas from Claude (vantage caption). Nothing here is used\n"
        "# until a person copies it into story.yaml; check every claim against the imagery first.\n"
    )
    doc = {"version": 1, "model": model, "prompt_version": PROMPT_VERSION, "vantages": out}
    path.write_text(
        header + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=100), encoding="utf-8"
    )
    log.info(f"{calls} Claude request(s), {hits} from cache ({cache_dir})")
    return path
