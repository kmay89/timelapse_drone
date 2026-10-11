"""Caption, chapter and transcript tracks of a film, made from its plan (film/tracks.py).

A plan's hashed `captions`, `chapters` and `transcript` are filled at plan time (film/story.py and
film/plan.py, with `Tracks`), with every fact already resolved and gated, so the tracks can never
drift from the picture or print a fact the release gate has not seen. This module only formats
them, and embeds them:

    <stem>.vtt            WebVTT captions (hh:mm:ss.mmm)
    <stem>.srt            SubRip captions (hh:mm:ss,mmm), for social uploads
    <stem>.chapters.vtt   WebVTT chapters
    <stem>.txt            a timed text transcript, the film's text alternative (WCAG 1.2.1)

`embed` remuxes a rendered MP4 by stream copy: the video stream is copied bit for bit and gains a
mov_text subtitle track (off by default) and named chapters. The mp4 muxer always enables the first
subtitle track, so `embed` then clears that track's tkhd `enabled` flag (one bit): players list the
captions but do not show them until asked. Never `-map_metadata -1` in that remux: chapter titles
are metadata, and it blanks them.
"""

from __future__ import annotations

import html
import re
import struct
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from vantage.media.ffmpeg import run
from vantage.models import FilmPlan

CPS = 17  # the most characters per second a caption cue may ask a reader to take in
SUFFIXES = (".vtt", ".srt", ".chapters.vtt", ".txt")
ISO639_2 = {"en": "eng", "fr": "fra", "es": "spa", "de": "deu"}
_END = re.compile(r"[.!?…:;]\s*$")
_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")


def iso639_2(lang: str | None) -> str:
    """project.yaml `lang` ('en', 'en-GB', 'fr_CA') → ISO 639-2 ('eng'), else 'und'."""
    base = re.split(r"[-_]", (lang or "").strip().lower(), maxsplit=1)[0]
    return ISO639_2.get(base, "und")


def one_line(text: str) -> str:
    """Whitespace collapsed to single spaces."""
    return _SPACE.sub(" ", text).strip()


def plain_markdown(text: str) -> str:
    """Inline markdown (already free of fact tokens) as plain text: '**Before.** Weeds' → 'Before. Weeds'."""
    from vantage.site.build import markdown

    return one_line(html.unescape(_TAG.sub("", markdown().renderInline(text))))


def sentence(*parts: str | None) -> str:
    """Parts joined as sentences: 'April 2025' + 'Before.' → 'April 2025. Before.' (no doubled stops)."""
    out = ""
    for part in parts:
        part = one_line(part or "")
        if not part:
            continue
        out = part if not out else f"{out}{' ' if _END.search(out) else '. '}{part}"
    return out


def fits(text: str, f0: int, f1: int, fps: int) -> bool:
    """Whether a cue of `text` from frame f0 to f1 stays at or under CPS characters per second."""
    return len(text) <= CPS * (f1 - f0) / fps


# --------------------------------------------------------------------------- #
# Plan time: collecting the tracks
# --------------------------------------------------------------------------- #


@dataclass
class Tracks:
    """Collects a plan's caption cues, chapter marks and transcript lines in film order (a disabled
    collector, for loop films or output.film.tracks: false, records nothing).

    A cue runs from its start to the next cue's start (the last to the film's end). `more` is the
    longer text a cue takes only when it still fits CPS characters per second (a capture's note, a
    curtain step's text)."""

    fps: int
    enabled: bool = True
    cues: list[dict[str, Any]] = field(default_factory=list)
    marks: list[dict[str, Any]] = field(default_factory=list)
    lines: list[dict[str, Any]] = field(default_factory=list)

    def cue(
        self,
        f0: int,
        text: str | None,
        facts: Sequence[str] = (),
        more: tuple[str, Sequence[str]] | None = None,
    ) -> None:
        if self.enabled and text:
            self.cues.append({"f0": f0, "text": text, "facts": list(facts), "more": more})

    def chapter(self, f0: int, title: str | None, facts: Sequence[str] = ()) -> None:
        if self.enabled and title:
            self.marks.append({"f0": f0, "title": one_line(title), "facts": list(dict.fromkeys(facts))})

    def line(self, f0: int, text: str | None, facts: Sequence[str] = ()) -> None:
        if self.enabled and text:
            self.lines.append({"f0": f0, "text": one_line(text), "facts": list(dict.fromkeys(facts))})

    def record(
        self, frames: int, *, title: str | None, subtitle: str | None, honesty: str, facts: Sequence[str] = ()
    ) -> dict[str, Any]:
        """plan.json `captions`, `chapters` and `transcript` (hashed). The first chapter starts at
        frame 0 (the title belongs to it); a chapter or cue that would last no frames is dropped, and
        consecutive cues with the same text become one."""
        if not self.enabled:
            return {"captions": [], "chapters": [], "transcript": None}
        cues = sorted(self.cues, key=lambda c: c["f0"])
        captions = []
        for k, cue in enumerate(cues):
            f1 = cues[k + 1]["f0"] if k + 1 < len(cues) else frames
            if f1 <= cue["f0"]:
                continue
            text, used = cue["text"], cue["facts"]
            if cue["more"] is not None and fits(cue["more"][0], cue["f0"], f1, self.fps):
                text, used = cue["more"][0], list(cue["more"][1])
            text = one_line(text)
            if captions and captions[-1]["text"] == text and captions[-1]["f1"] == cue["f0"]:
                captions[-1]["f1"] = f1  # the same words again (curtain steps): one longer cue
                captions[-1]["facts"] = list(dict.fromkeys([*captions[-1]["facts"], *used]))
                continue
            captions.append({"f0": cue["f0"], "f1": f1, "text": text, "facts": list(dict.fromkeys(used))})
        marks = sorted(self.marks, key=lambda m: m["f0"])
        if marks:
            marks[0] = {**marks[0], "f0": 0}
        chapters = [
            m
            for k, m in enumerate(marks)
            if m["f0"] < frames and (k + 1 == len(marks) or marks[k + 1]["f0"] > m["f0"])
        ]
        return {
            "captions": captions,
            "chapters": chapters,
            "transcript": {
                "title": title,
                "subtitle": subtitle,
                "honesty": honesty,
                "facts": list(dict.fromkeys(facts)),
                "lines": sorted(self.lines, key=lambda line: line["f0"]),
            },
        }


def track_facts(plan: dict[str, Any]) -> list[str]:
    """Every fact id the tracks of a plan print (captions, chapters, transcript)."""
    out: list[str] = []
    transcript = plan.get("transcript") or {}
    for item in [*plan.get("captions", []), *plan.get("chapters", []), *transcript.get("lines", [])]:
        out += item.get("facts", [])
    out += transcript.get("facts", [])
    return list(dict.fromkeys(out))


# --------------------------------------------------------------------------- #
# Formats
# --------------------------------------------------------------------------- #


def _ms(f: int, fps: int) -> int:
    """Frame f as whole milliseconds (rounded half up): frame 36 at 30 fps → 1200."""
    return (2000 * f + fps) // (2 * fps)


def _clock(ms: int, sep: str) -> str:
    h, rest = divmod(ms, 3_600_000)
    m, rest = divmod(rest, 60_000)
    s, ms = divmod(rest, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def timestamp(f: int, fps: int, *, srt: bool = False) -> str:
    """'00:00:01.200' (WebVTT) or '00:00:01,200' (SRT) for frame f."""
    return _clock(_ms(f, fps), "," if srt else ".")


def minutes(f: int, fps: int) -> str:
    """'m:ss' for frame f (whole seconds, rounded down)."""
    s = f // fps
    return f"{s // 60}:{s % 60:02d}"


def _vtt_text(text: str) -> str:
    return html.escape(text.replace("-->", "->"), quote=False)


def _ends(items: Sequence[dict[str, Any]], frames: int) -> list[tuple[int, int, dict[str, Any]]]:
    return [
        (item["f0"], item.get("f1", items[k + 1]["f0"] if k + 1 < len(items) else frames), item)
        for k, item in enumerate(items)
    ]


def to_vtt(plan: FilmPlan) -> str:
    p = plan.plan
    out = ["WEBVTT", ""]
    for f0, f1, cue in _ends(p.get("captions", []), p["frames"]):
        out += [f"{timestamp(f0, p['fps'])} --> {timestamp(f1, p['fps'])}", _vtt_text(cue["text"]), ""]
    return "\n".join(out) + "\n"


def to_srt(plan: FilmPlan) -> str:
    p = plan.plan
    out = []
    for k, (f0, f1, cue) in enumerate(_ends(p.get("captions", []), p["frames"]), 1):
        stamp = f"{timestamp(f0, p['fps'], srt=True)} --> {timestamp(f1, p['fps'], srt=True)}"
        out += [str(k), stamp, cue["text"].replace("-->", "->"), ""]
    return "\n".join(out) + "\n" if out else ""


def to_chapters_vtt(plan: FilmPlan) -> str:
    p = plan.plan
    out = ["WEBVTT", ""]
    for f0, f1, mark in _ends(p.get("chapters", []), p["frames"]):
        out += [f"{timestamp(f0, p['fps'])} --> {timestamp(f1, p['fps'])}", _vtt_text(mark["title"]), ""]
    return "\n".join(out) + "\n"


def _meta(text: str) -> str:
    return re.sub(r"([=;#\\\n])", r"\\\1", text)


def to_ffmetadata(plan: FilmPlan) -> str:
    """The chapters (and the film's title) as an ffmetadata file, in milliseconds."""
    p = plan.plan
    out = [";FFMETADATA1"]
    title = (p.get("transcript") or {}).get("title")
    if title:
        out.append(f"title={_meta(title)}")
    for f0, f1, mark in _ends(p.get("chapters", []), p["frames"]):
        out += [
            "",
            "[CHAPTER]",
            "TIMEBASE=1/1000",
            f"START={_ms(f0, p['fps'])}",
            f"END={_ms(f1, p['fps'])}",
            f"title={_meta(mark['title'])}",
        ]
    return "\n".join(out) + "\n"


def to_transcript(plan: FilmPlan) -> str:
    """The timed transcript: title and subtitle, '<m:ss> film, no sound. <honesty line>', then one
    '[m:ss] …' line per shot. It reads only the plan, whose text was printed through the facts."""
    p = plan.plan
    t = p.get("transcript") or {}
    fps = p["fps"]
    seconds = round(p["frames"] / fps)
    head = [x for x in (t.get("title"), t.get("subtitle")) if x]
    out = [*head, f"{seconds // 60}:{seconds % 60:02d} film, no sound. {t.get('honesty') or ''}".rstrip(), ""]
    out += [f"[{minutes(line['f0'], fps)}] {line['text']}" for line in t.get("lines", [])]
    return "\n".join(out) + "\n"


def track_texts(plan: FilmPlan) -> dict[str, str]:
    """suffix → contents of each sidecar a plan has ({} when it has no tracks)."""
    if not plan.encode.get("tracks") or plan.plan.get("transcript") is None:
        return {}
    return {
        ".vtt": to_vtt(plan),
        ".srt": to_srt(plan),
        ".chapters.vtt": to_chapters_vtt(plan),
        ".txt": to_transcript(plan),
    }


def write_tracks(plan: FilmPlan, stem: Path) -> list[Path]:
    """Write <stem>.vtt, .srt, .chapters.vtt and .txt (none for a film without tracks)."""
    out = []
    for suffix, text in track_texts(plan).items():
        path = stem.with_name(stem.name + suffix)
        path.write_text(text, encoding="utf-8", newline="\n")
        out.append(path)
    return out


# --------------------------------------------------------------------------- #
# Embedding
# --------------------------------------------------------------------------- #


def _boxes(data: bytearray, start: int, end: int):
    i = start
    while i + 8 <= end:
        size, kind = struct.unpack(">I4s", data[i : i + 8])
        head = 8
        if size == 1:
            size, head = struct.unpack(">Q", data[i + 8 : i + 16])[0], 16
        elif size == 0:
            size = end - i
        if size < head:
            raise ValueError("malformed MP4 box")
        yield i, head, size, kind
        i += size


def disable_subtitles(path: Path) -> int:
    """Clear the tkhd `enabled` flag of every subtitle ('sbtl') track in an MP4, so players offer the
    captions without showing them (ffprobe: disposition default 0). Returns the tracks changed."""
    data = bytearray(path.read_bytes())
    changed = 0
    for i, head, size, kind in _boxes(data, 0, len(data)):
        if kind != b"moov":
            continue
        for j, h2, s2, k2 in _boxes(data, i + head, i + size):
            if k2 != b"trak":
                continue
            tkhd, handler = None, None
            for k, h3, s3, k3 in _boxes(data, j + h2, j + s2):
                if k3 == b"tkhd":
                    tkhd = k + h3
                elif k3 == b"mdia":
                    for m, h4, _, k4 in _boxes(data, k + h3, k + s3):
                        if k4 == b"hdlr":
                            handler = bytes(data[m + h4 + 8 : m + h4 + 12])
            if handler == b"sbtl" and tkhd is not None and data[tkhd + 3] & 1:
                data[tkhd + 3] &= 0xFE
                changed += 1
    if changed:
        path.write_bytes(bytes(data))
    return changed


def embed(mp4: Path, plan: FilmPlan) -> None:
    """Remux `mp4` (a finished encode, usually <name>.mp4.part) in place by stream copy, adding the
    plan's captions as a mov_text track (language from encode.lang, off by default) and its named
    chapters. The H.264 stream is copied bit for bit. Nothing happens when encode.tracks is false."""
    p = plan.plan
    if not plan.encode.get("tracks") or not (p.get("captions") or p.get("chapters")):
        return
    lang = plan.encode.get("lang") or "und"
    with tempfile.TemporaryDirectory(prefix="vantage-tracks-") as tmp:
        meta, srt, out = Path(tmp) / "chapters.txt", Path(tmp) / "captions.srt", Path(tmp) / "out.mp4"
        meta.write_text(to_ffmetadata(plan), encoding="utf-8", newline="\n")
        args: list[str | Path] = ["-i", mp4, "-f", "ffmetadata", "-i", meta]
        maps: list[str | Path] = ["-map", "0:v"]
        if p.get("captions"):
            srt.write_text(to_srt(plan), encoding="utf-8", newline="\n")
            args += ["-i", srt]
            maps += ["-map", "2:s"]
        args += [*maps, "-map_chapters", "1", "-map_metadata", "1", "-c:v", "copy"]
        if p.get("captions"):
            args += ["-c:s", "mov_text", "-metadata:s:s:0", f"language={lang}", "-disposition:s:0", "0"]
        args += ["-fflags", "+bitexact", "-flags:v", "+bitexact", "-movflags", "+faststart", "-f", "mp4", out]
        run(args)
        disable_subtitles(out)
        out.replace(mp4)


__all__ = [
    "CPS",
    "SUFFIXES",
    "Tracks",
    "disable_subtitles",
    "embed",
    "fits",
    "iso639_2",
    "plain_markdown",
    "sentence",
    "to_chapters_vtt",
    "to_ffmetadata",
    "to_srt",
    "to_transcript",
    "to_vtt",
    "track_facts",
    "track_texts",
    "write_tracks",
]
