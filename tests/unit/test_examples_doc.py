# SPDX-License-Identifier: GPL-3.0-or-later
"""Every example in documentation/examples.md loads, validates, and compiles."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.example_documents import FILE_MARKER, examples
from ww.cli import main
from ww.config import load_configuration
from ww.extensions import ExtensionRegistry
from ww.plan import compile_workflow_plan
from ww.project_config import load_project_config

# A Markdown block whose first line names a file is a file the example uses,
# such as a rule file; it is written into the project before loading.
_FILE_HEADER = re.compile(r"<!-- (\S+) -->\n")
# Skills and slash commands the examples refer to; a project would discover
# them in its agent directory.
SKILLS = ("review-code",)
SLASH_COMMANDS = ("ship",)


def _check_layered(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    blocks: list[str],
) -> None:
    """Write each ``# file:`` block where its level reads it, then lint and plan."""
    user = root / "user"
    user.mkdir()
    monkeypatch.setenv("WW_USER_CONFIG_DIR", str(user))
    project = root / "project"
    project.mkdir()
    for text in blocks:
        marker = FILE_MARKER.match(text)
        assert marker is not None, "every block of a layered example names its file"
        name = marker.group(1)
        target = user / "ww.yaml" if name.startswith("~") else project / name
        target.write_text(text[marker.end() :], encoding="utf-8")
    assert main(["--root", str(project), "lint"]) == 0
    capsys.readouterr()
    assert main(["--root", str(project), "discover", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    for workflow in report["workflows"]:
        assert (
            main(
                [
                    "--root",
                    str(project),
                    "plan",
                    "--workflow",
                    workflow["name"],
                    "--agent",
                    "codex",
                ]
            )
            == 0
        )
        capsys.readouterr()


@pytest.mark.parametrize(
    ("number", "blocks"), examples(), ids=[number for number, _ in examples()]
)
def test_example_loads_and_compiles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    number: str,
    blocks: list[tuple[str, str]],
) -> None:
    yaml_blocks = [text for kind, text in blocks if kind == "yaml"]
    json_blocks = [text for kind, text in blocks if kind == "json"]
    for kind, text in blocks:
        header = _FILE_HEADER.match(text) if kind == "markdown" else None
        if header is not None:
            target = tmp_path / header.group(1)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text[header.end() :], encoding="utf-8")
    assert yaml_blocks or json_blocks, f"example {number} has no configuration"
    if any(FILE_MARKER.match(text) for text in yaml_blocks):
        _check_layered(tmp_path, monkeypatch, capsys, yaml_blocks)
        return
    skills = tmp_path / ".codex/skills"
    for name in SKILLS:
        (skills / name).mkdir(parents=True)
        (skills / name / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")
    commands = tmp_path / ".codex/commands"
    commands.mkdir(parents=True)
    for name in SLASH_COMMANDS:
        (commands / f"{name}.md").write_text(f"# /{name}\n", encoding="utf-8")
    for text in json_blocks:
        (tmp_path / "ww.json").write_text(text, encoding="utf-8")
        settings = load_project_config(tmp_path / "ww.json")
        for project in settings.projects:
            (tmp_path / project.path).mkdir(parents=True, exist_ok=True)
    extensions = ExtensionRegistry.discover(tmp_path)
    for text in yaml_blocks:
        config_file = tmp_path / "ww.yaml"
        config_file.write_text(text, encoding="utf-8")
        configuration = load_configuration(config_file, extensions)
        for workflow in configuration.workflows:
            plan = compile_workflow_plan(
                configuration, tmp_path, workflow.name, "codex", "TASK-1", extensions
            )
            assert plan.items, (number, workflow.name)


def test_examples_are_numbered_consecutively() -> None:
    numbers = [int(number) for number, _ in examples()]

    assert numbers == list(range(1, len(numbers) + 1))
