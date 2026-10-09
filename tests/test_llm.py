"""Claude caption drafts against a fake `anthropic` module: skips, request shape, cache, refusals."""

from __future__ import annotations

import base64
import io
import json
import re
import sys
import types
from collections.abc import Callable
from typing import Any

import pytest
import yaml
from PIL import Image

from test_film import DATES, make_project
from vantage import config
from vantage.llm import claude
from vantage.llm.claude import draft_captions
from vantage.models import MastersIndex


class FakeAPIError(Exception):
    pass


class FakeAuthError(FakeAPIError):
    pass


def reply_for(kwargs: dict[str, Any], **overrides: Any) -> types.SimpleNamespace:
    """A schema-valid answer whose caption names the visit the request asked about."""
    task = kwargs["messages"][0]["content"][-1]["text"]
    label = re.search(r"caption for ([A-Z][a-z]+ \d{4})", task).group(1)
    analysis = {
        "usable": True,
        "issues": [],
        "changes": [
            {"element": "new path", "description": "A path is visible.", "box": [0.6, 0.2, 0.2, 0.4]},
            {"element": "grass", "description": "Grass is greener.", "box": None},
        ],
        "milestones": [f"Path visible by {label}"],
        "caption": f"A new path is visible by {label}.",
        "alt_text": "Aerial view of a small site.",
        "alignment_ok": True,
        **overrides,
    }
    block = types.SimpleNamespace(type="text", text=json.dumps(analysis))
    thinking = types.SimpleNamespace(type="thinking", thinking="")
    return types.SimpleNamespace(stop_reason="end_turn", model=kwargs["model"], content=[thinking, block])


class FakeClient:
    def __init__(self, reply: Callable[[dict[str, Any]], Any], api_key: str | None) -> None:
        self.reply, self.api_key, self.calls = reply, api_key, []
        self.messages = types.SimpleNamespace(create=self.create)

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self.reply(kwargs)


def install(
    monkeypatch, reply=reply_for, *, structured: bool = True, api_key: str | None = "sk-test"
) -> FakeClient:
    client = FakeClient(reply, api_key)
    module = types.ModuleType("anthropic")
    module.Anthropic = lambda: client
    module.APIError = FakeAPIError
    module.AuthenticationError = FakeAuthError
    module.PermissionDeniedError = FakeAuthError
    if structured:
        module.transform_schema = lambda model: model.model_json_schema()
    monkeypatch.setitem(sys.modules, "anthropic", module)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("VANTAGE_CLAUDE_MODEL", raising=False)
    return client


def images(call: dict[str, Any]) -> list[Image.Image]:
    blocks = [b for b in call["messages"][0]["content"] if b["type"] == "image"]
    return [Image.open(io.BytesIO(base64.b64decode(b["source"]["data"]))) for b in blocks]


def load(path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_skips_without_sdk_or_credentials(tmp_path, monkeypatch, capsys):
    project = make_project(tmp_path / "proj")
    monkeypatch.setitem(sys.modules, "anthropic", None)  # import fails
    assert draft_captions(project) is None
    assert "uv sync --extra llm" in capsys.readouterr().err

    client = install(monkeypatch, api_key=None)
    assert draft_captions(project) is None
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err
    assert client.calls == [] and not (project.work_dir / "llm" / "suggestions.yaml").exists()


def test_drafts_suggestions_and_caches(tmp_path, monkeypatch):
    project = make_project(
        tmp_path / "proj",
        extra={"pier": list(DATES[:2])},
        story={
            "vantages": [{"id": "overview", "name": "Overview"}, {"id": "pier", "name": "The pier"}],
            "captures": [{"date": DATES[1], "note": "Paths go in."}],
            "chapters": [{"type": "text", "title": "Hello"}],
        },
    )
    (project.footage_dir / DATES[1]).mkdir(parents=True)
    (project.footage_dir / DATES[1] / "notes.txt").write_text("Crane on the east side.\n")
    monkeypatch.setattr(claude, "MAX_EDGE", 100)
    client = install(monkeypatch)

    path = draft_captions(project)

    assert path == project.work_dir / "llm" / "suggestions.yaml"
    doc = load(path)
    assert (doc["version"], doc["model"], doc["prompt_version"]) == (
        1,
        "claude-opus-5-5",
        claude.PROMPT_VERSION,
    )
    overview = doc["vantages"]["overview"]
    assert [e["id"] for e in overview] == [f"overview-{d}" for d in DATES]
    assert [e["compared_with"] for e in overview] == [None, DATES[0], DATES[1]]
    assert [e["label"] for e in overview] == ["June 2025", "August 2025", "January 2026"]
    assert {e["status"] for e in overview} == {"proposed"}
    second = overview[1]
    assert second["analysis"]["caption"] == second["step"]["text"] == "A new path is visible by August 2025."
    config.Step.model_validate(second["step"])
    (hotspot,) = second["hotspots"]  # the diffuse change has no box, so no hotspot
    assert hotspot == {
        "id": f"new-path-{DATES[1]}",
        "x": 0.4,
        "y": 0.3,
        "label": "New path",
        "body": "A path is visible.",
        "from": DATES[1],
    }
    config.Hotspot.model_validate(hotspot)
    assert second["analysis"]["changes"][0]["box"] == [0.2, 0.2, 0.6, 0.4]  # corners put in order
    assert len(doc["vantages"]["pier"]) == 2

    # Requests: first visit alone, then consecutive pairs, downscaled, with context and structured output.
    assert len(client.calls) == 5
    first, pair = client.calls[0], client.calls[1]
    assert [im.size for im in images(first)] == [(100, 63)]
    assert [im.size for im in images(pair)] == [(100, 63), (100, 63)]
    task = pair["messages"][0]["content"][-1]["text"]
    for expected in (
        "A Tiny Site",
        "Testville",
        "Paths go in.",
        "Crane on the east side.",
        "visible by August 2025",
    ):
        assert expected in task
    assert "A new path is visible by June 2025." in task  # the earlier caption, so it isn't repeated
    assert pair["output_config"]["format"]["type"] == "json_schema"
    assert pair["output_config"]["format"]["schema"]["required"][0] == "usable"
    assert pair["extra_body"] == {"fallbacks": "default"}
    assert pair["extra_headers"] == {"anthropic-beta": claude.FALLBACK_BETA}

    # A re-run is answered from the cache, byte for byte.
    before = path.read_bytes()
    assert len(list((project.work_dir / "llm" / "cache").glob("*.json"))) == 5
    assert draft_captions(project) == path
    assert len(client.calls) == 5 and path.read_bytes() == before

    # Another model is another cache key; it gets no fallback parameters.
    monkeypatch.setenv("VANTAGE_CLAUDE_MODEL", "claude-haiku-5-5")
    draft_captions(project)
    assert len(client.calls) == 10 and "extra_body" not in client.calls[-1]
    assert load(path)["model"] == "claude-haiku-5-5"


def test_refusals_and_bad_replies_are_reported_not_cached(tmp_path, monkeypatch):
    project = make_project(tmp_path / "proj")

    def reply(kwargs: dict[str, Any]) -> Any:
        task = kwargs["messages"][0]["content"][-1]["text"]
        if "August 2025" in task.split("\n\n")[-1]:
            details = types.SimpleNamespace(category="cyber", explanation=None)
            return types.SimpleNamespace(
                stop_reason="refusal", stop_details=details, model=kwargs["model"], content=[]
            )
        if "January 2026" in task.split("\n\n")[-1]:
            cut = types.SimpleNamespace(type="text", text='{"usable": tr')
            return types.SimpleNamespace(stop_reason="max_tokens", model=kwargs["model"], content=[cut])
        return reply_for(kwargs)

    client = install(monkeypatch, reply)
    entries = load(draft_captions(project))["vantages"]["overview"]

    assert [e["status"] for e in entries] == ["proposed", "refused", "failed"]
    assert entries[1]["problem"] == "refused (cyber)" and "analysis" not in entries[1]
    assert entries[2]["problem"].startswith("unparseable reply (stop_reason max_tokens)")
    assert len(list((project.work_dir / "llm" / "cache").glob("*.json"))) == 1
    draft_captions(project)
    assert len(client.calls) == 5  # only the two without an answer are asked again


def test_json_prompt_fallback_for_older_sdks(tmp_path, monkeypatch):
    project = make_project(tmp_path / "proj")

    def fenced(kwargs: dict[str, Any]) -> Any:
        response = reply_for(kwargs, caption="Word " * 80)
        response.content[-1].text = f"```json\n{response.content[-1].text}\n```"
        return response

    client = install(monkeypatch, fenced, structured=False)
    entries = load(draft_captions(project))["vantages"]["overview"]

    assert "output_config" not in client.calls[0] and "JSON schema" in client.calls[0]["system"]
    caption = entries[0]["analysis"]["caption"]
    assert (
        entries[0]["status"] == "proposed" and len(caption) <= claude.CAPTION_CHARS and caption.endswith("…")
    )


def test_api_errors(tmp_path, monkeypatch):
    project = make_project(tmp_path / "proj")

    def fail(error: Exception) -> Callable[[dict[str, Any]], Any]:
        def reply(kwargs: dict[str, Any]) -> Any:
            raise error

        return reply

    install(monkeypatch, fail(FakeAuthError("invalid x-api-key")))
    assert draft_captions(project) is None

    install(monkeypatch, fail(FakeAPIError("overloaded")))
    with pytest.raises(ConnectionError, match="re-run to resume"):
        draft_captions(project)


def test_a_new_flight_costs_one_request(tmp_path, monkeypatch):
    project = make_project(tmp_path / "proj")
    index_path = project.masters_dir / "index.json"
    full = index_path.read_text(encoding="utf-8")
    index = MastersIndex.load(index_path)
    index.vantages["overview"].captures.pop()  # the latest flight hasn't happened yet
    index.save(index_path)
    client = install(monkeypatch)

    draft_captions(project)
    assert len(client.calls) == 2

    index_path.write_text(full, encoding="utf-8")  # it has now
    entries = load(draft_captions(project))["vantages"]["overview"]
    assert len(client.calls) == 3 and "January 2026" in client.calls[-1]["messages"][0]["content"][-1]["text"]
    assert [e["status"] for e in entries] == ["proposed"] * 3


def test_survives_broken_cache_entries_and_non_utf8_notes(tmp_path, monkeypatch):
    project = make_project(tmp_path / "proj")
    (project.footage_dir / DATES[0]).mkdir(parents=True)
    (project.footage_dir / DATES[0] / "notes.txt").write_bytes(b"Caf\xe9 tables out.\n")  # Latin-1
    client = install(monkeypatch)
    path = draft_captions(project)
    before = path.read_bytes()
    assert "Caf� tables out." in client.calls[0]["messages"][0]["content"][-1]["text"]

    cache = sorted((project.work_dir / "llm" / "cache").glob("*.json"))
    cache[0].write_text('{"model": "claude-opus-5-5", "analy', encoding="utf-8")  # killed mid-write
    assert draft_captions(project) == path
    assert len(client.calls) == 4 and path.read_bytes() == before
    assert not list((project.work_dir / "llm" / "cache").glob("*.tmp"))


def test_hotspot_ids_are_unique_and_pixel_boxes_dropped(tmp_path, monkeypatch):
    project = make_project(tmp_path / "proj")
    changes = [
        {"element": "Path", "description": "A path.", "box": [0.1, 0.1, 0.3, 0.3]},
        {"element": "path", "description": "Another path.", "box": [0.5, 0.5, 0.7, 0.9]},
        {"element": "lot", "description": "A lot, boxed in pixels.", "box": [120, 40, 300, 200]},
    ]
    install(monkeypatch, lambda kwargs: reply_for(kwargs, changes=changes))
    entry = load(draft_captions(project))["vantages"]["overview"][1]

    assert [h["id"] for h in entry["hotspots"]] == [f"path-{DATES[1]}", f"path-{DATES[1]}-2"]
    assert entry["analysis"]["changes"][2]["box"] is None
    for hotspot in entry["hotspots"]:
        config.Hotspot.model_validate(hotspot)
