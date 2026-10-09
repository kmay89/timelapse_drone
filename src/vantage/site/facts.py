"""`{fact:id}` tokens: every number and claim in a story, printed from facts.yaml with numbered notes.

Story text says ``"Shovels went in on {fact:groundbreaking}."``; the build prints the fact's text
and, in markdown, a superscript note marker linking to the numbered source list in the credits.
Notes are numbered in first-use (reading) order and facts that are never used are ignored.
Markdown is rendered with raw HTML disabled, so facts are marked with private-use sentinels
before rendering (`FactNotes.mark`) and wrapped in their HTML afterwards (`FactNotes.finish`).
"""

from __future__ import annotations

import html
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from vantage.config import FACT_TOKEN_RE, Fact, Project

_OPEN, _MID, _CLOSE = "", "", ""
_SENTINELS = re.compile("[-]")
_MARKED = re.compile(f"{_OPEN}(\\d+){_MID}(.*?){_CLOSE}", re.S)


class FactError(ValueError):
    """A token names a fact that facts.yaml does not define."""


@dataclass
class FactNotes:
    """Resolves tokens for one build and numbers the facts in the order they are first used."""

    facts: dict[str, Fact]
    order: list[str] = field(default_factory=list)

    def number(self, fact_id: str) -> int:
        if fact_id not in self.facts:
            raise FactError(f"unknown fact {{fact:{fact_id}}}: define {fact_id!r} in facts.yaml")
        if fact_id not in self.order:
            self.order.append(fact_id)
        return self.order.index(fact_id) + 1

    def plain(self, text: str | None) -> str | None:
        """Tokens → the fact's text, without a marker (titles, labels, meta). The fact is still noted."""
        if text is None:
            return None
        return FACT_TOKEN_RE.sub(lambda m: self.facts[self._id(m)].text, text)

    def mark(self, text: str) -> str:
        """Tokens → sentinel-wrapped fact text, ready for markdown; pass the rendered HTML to `finish`."""

        def sub(m: re.Match[str]) -> str:
            n = self.number(m.group(1))
            return f"{_OPEN}{n}{_MID}{self.facts[m.group(1)].text}{_CLOSE}"

        return FACT_TOKEN_RE.sub(sub, _SENTINELS.sub("", text))

    def finish(self, rendered: str) -> str:
        """Wrap each marked fact in rendered HTML: `<span class="v-fact">text</span><sup>n</sup>`."""

        def sub(m: re.Match[str]) -> str:
            n = int(m.group(1))
            fact_id = self.order[n - 1]
            fact = self.facts[fact_id]
            ok = fact.releasable
            title = "" if ok else f' title="Unverified: {html.escape(fact.status)}"'
            return (
                f'<span class="v-fact" data-fact="{fact_id}" data-status="{fact.status}"'
                f' data-releasable="{str(ok).lower()}"{title}>{m.group(2)}</span>'
                f'<sup class="v-note-ref"><a href="#v-note-{n}" aria-label="Note {n}">{n}</a></sup>'
            )

        return _MARKED.sub(sub, rendered)

    def notes(self) -> list[dict[str, Any]]:
        """StoryJSON `notes`: one entry per fact used, numbered in first-use order."""
        return [
            {
                "n": i,
                "factId": fact_id,
                "text": self.facts[fact_id].text,
                "sources": list(self.facts[fact_id].sources),
                "status": self.facts[fact_id].status,
            }
            for i, fact_id in enumerate(self.order, 1)
        ]

    def unverified(self) -> int:
        return sum(not self.facts[fact_id].releasable for fact_id in self.order)

    def _id(self, m: re.Match[str]) -> str:
        self.number(m.group(1))
        return m.group(1)


def _strings(value: Any, where: str) -> Iterator[tuple[str, str]]:
    if isinstance(value, str):
        yield where, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(item, f"{where}.{key}")
    elif isinstance(value, list):
        for i, item in enumerate(value):
            yield from _strings(item, f"{where}[{i}]")


def _reason(fact: Fact) -> str:
    if fact.status == "reported":
        return "is 'reported' without an attribution"
    return f"has status {fact.status!r}"


def check_release(project: Project) -> list[str]:
    """Why this project cannot ship as a release build (empty when it can)."""
    problems = (
        ["project.yaml has draft: true (set it to false for the release)"] if project.config.draft else []
    )
    seen: set[str] = set()
    texts = [
        *_strings(project.config.model_dump(mode="json", by_alias=True), "project.yaml"),
        *_strings(project.story.model_dump(mode="json", by_alias=True), "story.yaml"),
    ]
    for where, text in texts:
        for fact_id in FACT_TOKEN_RE.findall(text):
            if fact_id in seen:
                continue
            seen.add(fact_id)
            fact = project.facts.facts.get(fact_id)
            if fact is None:
                problems.append(f"{where}: unknown fact {{fact:{fact_id}}} (not in facts.yaml)")
            elif not fact.releasable:
                problems.append(f"{where}: fact {fact_id!r} {_reason(fact)}")
    return problems
