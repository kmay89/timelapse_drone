"""{fact:id} tokens: inline rendering, first-use numbering, unknown ids and the release gate."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from vantage.config import Facts, load_project
from vantage.site.build import markdown
from vantage.site.facts import WITHHELD, FactError, FactNotes, check_facts, check_release
from vantage.site.render import _env

FACTS = Facts.model_validate({
    "facts": {
        "date": {"text": "Oct. 23, 2025", "status": "verified", "sources": ["https://example.org/a"]},
        "price": {"text": "$5 million", "status": "needs-client"},
        "acres": {"text": "48 *acres*", "status": "reported", "attribution": "City"},
        "rumor": {"text": "a rumor", "status": "reported"},
        "unused": {"text": "never printed", "status": "do-not-print"},
        "secret": {"text": "$41.7 million", "status": "do-not-print", "sources": ["internal cost report v3"]},
    }
})  # fmt: skip


def render(notes: FactNotes, text: str) -> str:
    return notes.finish(markdown().render(notes.mark(text))).strip()


def test_tokens_render_text_and_numbered_notes():
    notes = FactNotes(FACTS.facts)
    html = render(
        notes, "**Broke ground {fact:date}.** It cost {fact:price}, on {fact:acres}; again {fact:date}."
    )
    assert html.startswith(
        '<p><strong>Broke ground <span class="v-fact" data-fact="date" data-status="verified"'
    )
    assert 'data-releasable="true">Oct. 23, 2025</span><sup class="v-note-ref"><a href="#v-note-1"' in html
    assert 'data-releasable="false" title="Unverified: needs-client">$5 million</span>' in html
    assert (
        '>48 <em>acres</em></span><sup class="v-note-ref"><a href="#v-note-3"' in html
    )  # fact text is markdown too
    assert html.count('href="#v-note-1"') == 2  # a repeated fact keeps its number
    assert [n["factId"] for n in notes.notes()] == ["date", "price", "acres"]
    assert notes.notes()[0] == {
        "n": 1, "factId": "date", "text": "Oct. 23, 2025", "sources": ["https://example.org/a"], "status": "verified",
        "releasable": True,
    }  # fmt: skip
    assert [n["releasable"] for n in notes.notes()] == [True, False, True]  # reported + attribution is fine
    assert notes.unverified() == 1
    notes.plain("{fact:rumor}")
    assert notes.notes()[-1]["releasable"] is False and notes.unverified() == 2  # reported, no attribution


def test_do_not_print_is_withheld_in_every_build():
    """A do-not-print fact never prints its text or sources, even in a draft (review) build."""
    notes = FactNotes(FACTS.facts)
    body = render(notes, "It cost {fact:secret}, opened {fact:date}.")
    title = notes.plain("Cost: {fact:secret}")
    refs = _env().get_template("refs.html").render(notes=notes.notes())
    for out in (body, title, refs, str(notes.notes())):
        assert "41.7" not in out and "internal cost report" not in out
    assert (
        '<span class="v-fact" data-fact="secret" data-status="do-not-print" data-releasable="false"'
        f' title="Unverified: do-not-print">{WITHHELD}</span><sup class="v-note-ref"><a href="#v-note-1"'
    ) in body  # still marked and numbered, so reviewers see the gap
    assert title == f"Cost: {WITHHELD}"
    assert notes.notes()[0] == {
        "n": 1, "factId": "secret", "text": WITHHELD, "sources": [], "status": "do-not-print",
        "releasable": False,
    }  # fmt: skip
    assert "Unverified: do not print" in refs and notes.unverified() == 1
    assert notes.notes()[1]["sources"] == ["https://example.org/a"]  # other facts keep their sources
    after = render(FactNotes(FACTS.facts), "Spent {fact:secret}(2024).")
    assert after.endswith("</sup>(2024).</p>") and 'href="2024"' not in after  # brackets never form a link


def test_plain_contexts_resolve_without_markup():
    notes = FactNotes(FACTS.facts)
    assert notes.plain("Opened {fact:date}") == "Opened Oct. 23, 2025"
    assert notes.plain(None) is None
    assert notes.order == ["date"]


def test_sentinels_in_source_text_are_stripped():
    notes = FactNotes(FACTS.facts)
    assert render(notes, "a9bc") == "<p>a9bc</p>"


def test_unknown_fact_is_an_error():
    with pytest.raises(FactError, match=r"unknown fact \{fact:nope\}"):
        FactNotes(FACTS.facts).mark("{fact:nope}")


def _project(root: Path, *, draft: bool, body: str, brand: dict[str, str] | None = None) -> Path:
    root.mkdir()
    (root / "brand").mkdir()
    (root / "brand" / "brand.yaml").write_text(yaml.safe_dump({"name": "X", **(brand or {})}))
    (root / "project.yaml").write_text(
        yaml.safe_dump({"slug": "x", "title": "X {fact:date}", "draft": draft})
    )
    story = {"vantages": [{"id": "a", "name": "A"}], "chapters": [{"type": "text", "body": body}]}
    (root / "story.yaml").write_text(yaml.safe_dump(story))
    (root / "facts.yaml").write_text(yaml.safe_dump(FACTS.model_dump(mode="json")))
    return root


def test_check_release(tmp_path: Path):
    ok = load_project(_project(tmp_path / "ok", draft=False, body="On {fact:acres}, {fact:date}."))
    assert check_release(ok) == []  # unused do-not-print facts don't matter

    bad = load_project(
        _project(tmp_path / "bad", draft=True, body="{fact:price} {fact:rumor} {fact:ghost} {fact:price}")
    )
    problems = check_release(bad)
    assert problems[0].startswith("project.yaml has draft: true")
    assert "story.yaml.chapters[0].body: fact 'price' has status 'needs-client'" in problems
    assert "story.yaml.chapters[0].body: fact 'rumor' is 'reported' without an attribution" in problems
    assert "story.yaml.chapters[0].body: unknown fact {fact:ghost} (not in facts.yaml)" in problems
    assert len(problems) == 4  # each fact reported once


def test_brand_text_is_part_of_the_release_gate(tmp_path: Path):
    brand = {
        "credit_line": "Flown since {fact:date}",
        "copyright": "© {fact:ghost-year} X",
        "disclaimer": "{fact:rumor}, {fact:price}.",
    }
    project = load_project(_project(tmp_path / "p", draft=False, body="On {fact:acres}.", brand=brand))
    unknown, unreleasable = check_facts(project)
    assert unknown == ["brand.yaml.copyright: unknown fact {fact:ghost-year} (not in facts.yaml)"]
    assert unreleasable == [
        "brand.yaml.disclaimer: fact 'rumor' is 'reported' without an attribution",
        "brand.yaml.disclaimer: fact 'price' has status 'needs-client'",
    ]
    assert check_release(project) == unknown + unreleasable  # date (verified) and acres pass

    ok = load_project(_project(tmp_path / "q", draft=False, body="x", brand={"credit_line": "{fact:date}"}))
    assert check_release(ok) == []
