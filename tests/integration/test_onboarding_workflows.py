# SPDX-License-Identifier: GPL-3.0-or-later
"""ww's learning and setup workflows, and the skills that start them."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.workflow_helpers import start_after_init
from ww.builtin_workflows import builtin_workflow, is_builtin
from ww.cli import main
from ww.config import load_configuration
from ww.config_files import user_directory
from ww.defaults import SKILLS
from ww.executable import printed_executable
from ww.extensions import ExtensionRegistry
from ww.service import WorkflowService
from ww.storage import Storage
from ww.variables import EXECUTABLE, runtime_variable_values
from ww.workflow_config import StepDefinition, WorkflowConfiguration

ONBOARDING = (
    "ww-learn",
    "ww-learn-project",
    "ww-suggest",
    "ww-solve",
    "ww-rules-from-artifacts",
    "ww-automate",
)
LEARNING_DOCUMENTS = ("me", "team", "company", "project")
REMARK = (
    "<!-- This file is maintained by ww for ww's own use. Do not use it for "
    "anything else. If you are an agent that is not doing ww work, ignore this "
    "file. -->"
)
NEW_SKILLS = (
    "ww-setup",
    "ww-learn",
    "ww-learn-project",
    "ww-suggest",
    "ww-refresh",
    "ww-solve",
    "ww-rules-from-artifacts",
    "ww-automate",
)

pytestmark = pytest.mark.usefixtures("shipped_builtins")


def _project(root: Path, settings: dict[str, object] | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "ww.yaml").write_text(
        "workflows:\n  - name: task\n    steps:\n      - work: Work.\n",
        encoding="utf-8",
    )
    (root / "ww.json").write_text(
        json.dumps(settings or {}), encoding="utf-8"
    )
    return root


def _load(root: Path) -> WorkflowConfiguration:
    return load_configuration(
        root / "ww.yaml", ExtensionRegistry.discover(root)
    )


def _steps(steps: tuple[StepDefinition, ...]) -> Iterator[StepDefinition]:
    for step in steps:
        yield step
        yield from _steps(step.child_steps)
        yield from _steps(step.loop_steps)
        yield from _steps(step.assessment_outcomes)


@pytest.mark.parametrize("workflow", ONBOARDING)
@pytest.mark.parametrize("agent", ["claudecode", "codex"])
def test_each_learning_workflow_plans_and_lints(
    tmp_path: Path, workflow: str, agent: str
) -> None:
    root = _project(tmp_path / "project")

    assert main(["--root", str(root), "lint"]) == 0
    assert (
        main(["--root", str(root), "plan", "-w", workflow, "--agent", agent]) == 0
    )


def test_the_learning_workflows_neither_branch_nor_commit() -> None:
    for name in ONBOARDING:
        workflow = builtin_workflow(name)
        assert workflow.runtime == "single"
        assert not workflow.hooks
        for step in _steps(workflow.steps):
            assert step.action is None or step.action.identifier not in {
                "argv",
                "shell",
            }


def test_every_learning_file_is_written_with_the_remark() -> None:
    written: dict[str, list[str]] = {}
    for name in ONBOARDING:
        for step in _steps(builtin_workflow(name).steps):
            for update in step.update_document:
                written.setdefault(update.name, []).append(update.instruction)

    for document in LEARNING_DOCUMENTS:
        assert written[document], document
        for instruction in written[document]:
            assert REMARK in instruction, document


def test_learning_documents_resolve_to_the_user_directory_and_project_root(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path / "project")
    documents = _load(root).documents_by_name
    service = WorkflowService(Storage(root))
    store = service.documents
    # A task working in a Git worktree still shares the project's files.
    worktree = tmp_path / "project" / "ww-worktrees" / "T-1"

    assert documents["me"].scope == "user"
    assert (
        store.path(documents["me"], "T-1", worktree)
        == (user_directory() / "me.md").resolve()
    )
    for name in ("team", "company", "project"):
        assert documents[name].scope == "project"
        expected = (root / ".ww" / f"{name}.md").resolve()
        assert store.path(documents[name], "T-1", worktree) == expected
        assert store.path(documents[name], None) == expected


def test_steps_name_ww_commands_with_the_configured_executable(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path / "project")
    service = WorkflowService(Storage(root))

    with printed_executable("ww-next"):
        start_after_init(service, "ww-suggest", "T-1", agent="codex")
        gather = service.next("T-1")

    assert gather.item_name == "gather"
    assert "`ww-next discover`" in gather.action_text
    assert "{{" not in gather.action_text
    with printed_executable(None):
        values = runtime_variable_values(root, "T-1")
    assert values[EXECUTABLE] == "./ww"


def test_discover_lists_the_learning_workflows_briefly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path / "project")

    assert main(["--root", str(root), "discover", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert [item["name"] for item in report["builtin_workflows"]] == list(
        ONBOARDING
    )
    assert "task" in [item["name"] for item in report["workflows"]]

    assert main(["--root", str(root), "discover"]) == 0
    output = capsys.readouterr().out
    section = output.split("## ww's own workflows", 1)[1].split("\n## ", 1)[0]
    entries = [line for line in section.splitlines() if line.startswith("- ")]
    assert [entry.split("`")[1] for entry in entries] == list(ONBOARDING)
    # One short line each.
    assert all(len(entry) < 140 for entry in entries)


@pytest.mark.parametrize("workflow", ONBOARDING)
def test_the_settings_switch_each_learning_workflow_off(
    tmp_path: Path, workflow: str
) -> None:
    root = _project(
        tmp_path / "project", {"workflows": {workflow: {"enabled": False}}}
    )

    configuration = _load(root)
    names = {item.name for item in configuration.workflows}

    assert workflow not in names
    assert set(ONBOARDING) - {workflow} <= names
    # A recommendation of the switched-off workflow is dropped, and the one
    # that made it is still listed as ww's own.
    for item in configuration.workflows:
        assert item.recommended_next_workflow in (None, *names)
        if item.name in ONBOARDING:
            assert is_builtin(item)


def test_switching_every_learning_workflow_off_drops_its_documents(
    tmp_path: Path,
) -> None:
    root = _project(
        tmp_path / "project",
        {"workflows": {name: {"enabled": False} for name in ONBOARDING}},
    )

    configuration = _load(root)

    assert not set(ONBOARDING) & {item.name for item in configuration.workflows}
    assert not set(LEARNING_DOCUMENTS) & set(configuration.documents_by_name)
    assert "ww-narrate" not in {mode.name for mode in configuration.modes}
    assert "catchall" in configuration.workflows_by_name


def test_init_installs_the_setup_skills(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / ".claude").mkdir()

    assert main(["--root", str(tmp_path), "init", "--skills", "--no-input"]) == 0
    output = capsys.readouterr().out

    assert set(NEW_SKILLS) <= set(SKILLS)
    for name in NEW_SKILLS:
        skill = tmp_path / ".claude" / "skills" / name / "SKILL.md"
        assert skill.read_text(encoding="utf-8") == SKILLS[name]
        assert SKILLS[name].startswith(f"---\nname: {name}\ndescription: ")
        assert f".claude/skills/{name}/SKILL.md" in output


@pytest.mark.parametrize(
    ("skill", "workflow"),
    [(name, name) for name in ONBOARDING if name != "ww-refresh"],
)
def test_each_skill_starts_its_workflow(skill: str, workflow: str) -> None:
    assert f"--workflow {workflow} " in SKILLS[skill]
    assert "--mode ww-narrate" in SKILLS[skill]


def test_the_setup_skill_guides_and_records_the_state() -> None:
    text = SKILLS["ww-setup"]

    for workflow in ("ww-learn", "ww-learn-project", "ww-suggest"):
        assert f"`{workflow}`" in text
    assert "onboarding --set explain=true" in text
    assert "onboarding --set setup.done=true" in text
