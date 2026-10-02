# SPDX-License-Identifier: GPL-3.0-or-later
"""``ww-scriptize-rules``: scriptizing rules as a project task of its own."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from ww.builtin_workflows import builtin_workflow, is_builtin
from ww.cli import main
from ww.config import load_configuration
from ww.defaults import SKILLS
from ww.errors import ConfigurationError
from ww.extensions import ExtensionRegistry
from ww.project_config import load_project_config
from ww.service import WorkflowService
from ww.storage import Storage
from ww.workflow_config import StepDefinition

NAME = "ww-scriptize-rules"
LANE_HOOK = """hooks:
  before_start_workflow:
    - workflows: [task]
      argv: [sh, -c, "echo lane > lane-hook.txt"]
workflows:
  - name: task
    steps:
      - develop: Do it.
"""

pytestmark = pytest.mark.usefixtures("shipped_builtins")


def _project(root: Path, settings: dict[str, object] | None = None) -> Path:
    (root / "ww.yaml").write_text(LANE_HOOK, encoding="utf-8")
    (root / "ww.json").write_text(json.dumps(settings or {}), encoding="utf-8")
    return root


def _steps(steps: tuple[StepDefinition, ...]) -> Iterator[StepDefinition]:
    for step in steps:
        yield step
        yield from _steps(step.child_steps)
        yield from _steps(step.assessment_outcomes)


def _step(name: str) -> StepDefinition:
    return next(s for s in _steps(builtin_workflow(NAME).steps) if s.name == name)


def _start(root: Path, task_id: str = "S-1") -> WorkflowService:
    service = WorkflowService(Storage(root))
    service.start(
        NAME, task_id, agent="codex", workflow_runtime="single", init_artifact="All."
    )
    return service


def test_the_workflow_collects_agrees_builds_and_records() -> None:
    workflow = builtin_workflow(NAME)
    names = [step.name for step in _steps(workflow.steps)]

    assert [name for name in names if name != "assess"] == [
        "collect",
        "approaches",
        "scriptize",
        "build",
        "checks",
    ]
    assert workflow.needs_hooks_from and workflow.hooks_from is None
    assert workflow.restartable
    assert _step("approaches").interactive and _step("checks").interactive
    assert [c.label for c in _step("approaches").choices] == ["build", "nothing to do"]
    collect = _step("collect").description
    for piece in ("rules --json", "`unscriptized`", "`check_guidance`", "fewest"):
        assert piece in collect, piece
    build = _step("build").description
    for piece in (
        "deliberately violating input",
        "a sample of real files",
        "real violations do not block",
        "baseline",
        "config files",
    ):
        assert piece in build, piece
    checks = _step("checks").description
    assert "rules convert <check> --covers" in checks
    assert "--dry-run" in checks and "--yes" in checks
    assert "rules decline" in checks
    assert "Never edit `ww-rule-automation.json` yourself" in checks


def test_it_refuses_to_start_without_a_lane(tmp_path: Path) -> None:
    root = _project(tmp_path)

    with pytest.raises(ConfigurationError, match="hooks_from"):
        _start(root)


def test_it_runs_with_the_lanes_hooks(tmp_path: Path) -> None:
    root = _project(tmp_path, {"workflows": {NAME: {"hooks_from": "task"}}})

    service = _start(root)

    _, snapshot = service.load("S-1")
    assert snapshot.plan.items[0].phase == "before_start_workflow"
    assert (root / "lane-hook.txt").read_text().strip() == "lane"
    configuration = load_configuration(
        root / "ww.yaml", ExtensionRegistry.discover(root)
    )
    workflow = configuration.workflows_by_name[NAME]
    assert workflow.hooks_from == "task"
    assert is_builtin(workflow)


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("nope", "takes its hooks from unknown workflow 'nope'"),
        (NAME, "cannot take its hooks from itself"),
    ],
)
def test_a_bad_lane_is_refused(tmp_path: Path, source: str, message: str) -> None:
    root = _project(tmp_path, {"workflows": {NAME: {"hooks_from": source}}})

    with pytest.raises(ConfigurationError, match=message):
        load_configuration(root / "ww.yaml", ExtensionRegistry.discover(root))


def test_a_lane_that_takes_hooks_itself_is_refused(tmp_path: Path) -> None:
    root = _project(tmp_path, {"workflows": {NAME: {"hooks_from": "copy"}}})
    (root / "ww.yaml").write_text(
        LANE_HOOK + "  - name: copy\n    hooks_from: task\n    steps:\n"
        "      - work: Work.\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="name that one"):
        load_configuration(root / "ww.yaml", ExtensionRegistry.discover(root))


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ({"hooks_from": ""}, "hooks_from must name a workflow"),
        ({"hooks_from": 3}, "hooks_from must name a workflow"),
        ({"lane": "task"}, r"unknown key\(s\): lane"),
    ],
)
def test_the_setting_is_validated(
    tmp_path: Path, value: dict[str, object], message: str
) -> None:
    path = tmp_path / "ww.json"
    path.write_text(json.dumps({"workflows": {NAME: value}}), encoding="utf-8")

    with pytest.raises(ConfigurationError, match=message):
        load_project_config(path)


def test_a_configured_workflow_may_take_a_lanes_hooks(tmp_path: Path) -> None:
    root = tmp_path
    (root / "ww.yaml").write_text(
        LANE_HOOK
        + "  - name: chores\n    hooks_from: task\n    steps:\n      - work: Work.\n",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(root))

    service.start(
        "chores", "C-1", agent="codex", workflow_runtime="single", init_artifact="Do."
    )

    assert (root / "lane-hook.txt").exists()


def test_the_skill_starts_the_workflow() -> None:
    skill = SKILLS["ww-scriptize"]

    assert skill.startswith("---\nname: ww-scriptize\ndescription: ")
    assert f"--workflow {NAME} " in skill
    assert "never edit ww's configuration files yourself" in " ".join(skill.split())


def test_discover_lists_it(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _project(tmp_path)

    assert main(["--root", str(root), "discover"]) == 0

    assert f"`{NAME}`" in capsys.readouterr().out
