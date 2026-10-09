"""Docs, skills and workflows stay true to the code: links resolve, every `vantage <command>`, script
and npm script they mention exists, and skill/agent frontmatter is well-formed."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest
import yaml

from vantage.cli import app
from vantage.paths import repo_root

ROOT = repo_root()
DOCS = sorted(
    [
        ROOT / "README.md",
        ROOT / "CLAUDE.md",
        ROOT / "web-tests" / "README.md",
        *(ROOT / "docs").rglob("*.md"),
        *(ROOT / ".claude").rglob("*.md"),
    ]
)
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
TEXTS = [*DOCS, *WORKFLOWS]
COMMANDS = {c.name for c in app.registered_commands}
NPM_SCRIPTS = set(json.loads((ROOT / "package.json").read_text())["scripts"])

_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
_VANTAGE = re.compile(r"(?:uv run |`)vantage ([a-z][a-z-]*)")
_SCRIPT = re.compile(r"\bscripts/([\w.-]+\.(?:py|sh))")
_NPM_RUN = re.compile(r"\bnpm run ([\w:-]+)")


def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _frontmatter(path: Path) -> dict[str, object]:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{_rel(path)} has no YAML frontmatter"
    return yaml.safe_load(text.split("---\n", 2)[1])


@pytest.mark.parametrize("doc", DOCS, ids=_rel)
def test_relative_links_resolve(doc: Path) -> None:
    for target in _LINK.findall(doc.read_text(encoding="utf-8")):
        if re.match(r"^(https?:|mailto:|#)", target):
            continue
        assert (doc.parent / target.split("#")[0]).exists(), f"{_rel(doc)}: broken link {target}"


@pytest.mark.parametrize("path", TEXTS, ids=_rel)
def test_mentioned_commands_and_scripts_exist(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert set(_VANTAGE.findall(text)) <= COMMANDS, f"{_rel(path)}: unknown vantage command"
    assert {s for s in _SCRIPT.findall(text) if not (ROOT / "scripts" / s).is_file()} == set()
    assert set(_NPM_RUN.findall(text)) <= NPM_SCRIPTS, f"{_rel(path)}: unknown npm script"


@pytest.mark.parametrize("skill", sorted((ROOT / ".claude" / "skills").glob("*/SKILL.md")), ids=_rel)
def test_skill_frontmatter(skill: Path) -> None:
    meta = _frontmatter(skill)
    assert meta["name"] == skill.parent.name
    assert len(str(meta["description"])) > 80, "the description is what triggers the skill"
    if skill.parent.name == "vantage-publish":
        assert meta.get("disable-model-invocation") is True, "publishing runs only when asked"


@pytest.mark.parametrize("agent", sorted((ROOT / ".claude" / "agents").glob("*.md")), ids=_rel)
def test_agent_frontmatter(agent: Path) -> None:
    meta = _frontmatter(agent)
    assert meta["name"] == agent.stem
    assert meta["description"] and meta["tools"]


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=_rel)
def test_workflows_parse_and_pin_actions(workflow: Path) -> None:
    spec = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    assert spec[True] and spec["jobs"]  # YAML 1.1 reads the `on:` key as True
    assert spec["permissions"] == {"contents": "read"}, "least privilege by default"
    for job in spec["jobs"].values():
        for step in job["steps"]:
            assert "uses" not in step or re.search(r"@v\d", step["uses"]), step["uses"]


def test_session_start_hook_is_wired_and_executable() -> None:
    settings = json.loads((ROOT / ".claude" / "settings.json").read_text())
    command = settings["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    script = ROOT / command.replace('"$CLAUDE_PROJECT_DIR"/', "")
    assert script.is_file() and os.access(script, os.X_OK)


def test_claude_md_stays_short() -> None:
    assert len((ROOT / "CLAUDE.md").read_text(encoding="utf-8").splitlines()) < 150
