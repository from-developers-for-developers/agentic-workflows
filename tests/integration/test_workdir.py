# SPDX-License-Identifier: GPL-3.0-or-later
"""``workdir``: steps and handlers that work outside the task workspace."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests.workflow_helpers import start_after_init
from ww.errors import ConfigurationError
from ww.service import WorkflowService
from ww.storage import Storage

WORKFLOWS = """handlers:
  - name: where
    argv: [pwd]
  - name: where-root
    argv: [pwd]
    workdir: root
workflows:
  - name: feature
    steps:
      - develop: Work in {{ww.task.workspace_dir}}.
        workdir: root
        hooks:
          after_complete:
            - where: ~
            - where-root: ~
            - where: ~
              workdir: project
  - name: nested
    steps:
      - outer:
        workdir: root
        steps:
          - inherits: Work in {{ww.task.workspace_dir}}.
          - overrides: Work in {{ww.task.workspace_dir}}.
            workdir: task
  - name: automatic
    steps:
      - run:
        argv: [pwd]
        workdir: project
      - run-in-task:
        argv: [pwd]
"""


def _root(tmp_path: Path, workflows: str = WORKFLOWS) -> Path:
    (tmp_path / "backend").mkdir()
    (tmp_path / "ww.yaml").write_text(workflows, encoding="utf-8")
    (tmp_path / "ww.json").write_text(
        json.dumps(
            {
                "enabled": True,
                "projects": [{"name": "backend", "path": "./backend"}],
                "extensions": {},
            }
        ),
        encoding="utf-8",
    )
    return tmp_path


def _outputs(service: WorkflowService, task_id: str) -> list[str]:
    return [
        service.tasks.read_command_output(entry["command_output"]).strip()
        for entry in service.artifacts(task_id)
        if entry.get("stream") == "stdout"
    ]


def _finish(service: WorkflowService, task_id: str) -> None:
    instruction = service.status(task_id)
    while instruction.status not in {"completed", "failed"}:
        assert instruction.error is None, instruction.error
        if instruction.item_status == "pending":
            instruction = service.next(task_id)
            continue
        instruction = service.complete(
            task_id,
            variables=tuple(
                (value.name, "done") for value in instruction.required_values
            ),
            artifact=None if instruction.status == "awaiting_input" else "x",
            summary_for_next="Done.",
        )
    assert instruction.status == "completed"


def test_a_root_step_works_in_the_root_while_its_hooks_choose_their_own(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path).resolve()
    service = WorkflowService(Storage(root))

    start_after_init(service, "feature", "T1", agent="codex", project="backend")
    develop = service.next("T1")
    service.complete("T1", artifact="done", summary_for_next="Done.")

    assert develop.working_directory == str(root)
    assert develop.action_text == f"Work in {root}."
    # A hook does not inherit the step's directory: ``where`` stays in the
    # task workspace, the others use the one they or their handler declare.
    assert _outputs(service, "T1") == [
        str(root / "backend"),
        str(root),
        str(root / "backend"),
    ]


def test_a_root_step_names_the_root_even_for_a_task_in_the_root(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path).resolve()
    service = WorkflowService(Storage(root))

    start_after_init(service, "feature", "T1", agent="codex")
    develop = service.next("T1")
    service.complete("T1", artifact="done", summary_for_next="Done.")

    assert develop.working_directory == str(root)
    # ``project`` without a project is the root.
    assert _outputs(service, "T1") == [str(root)] * 3


def test_nested_steps_inherit_workdir_unless_they_set_their_own(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path).resolve()
    service = WorkflowService(Storage(root))

    start_after_init(service, "nested", "T1", agent="codex", project="backend")
    inherits = service.next("T1")
    service.complete("T1", artifact="done", summary_for_next="Done.")
    overrides = service.next("T1")

    assert inherits.working_directory == str(root)
    assert inherits.action_text == f"Work in {root}."
    assert overrides.working_directory == str(root / "backend")
    assert overrides.action_text == f"Work in {root / 'backend'}."
    assert [item.workdir for item in _plan_items(service, "T1", "nested")] == [
        "root",
        "task",
    ]


def test_an_automatic_step_runs_in_its_workdir(tmp_path: Path) -> None:
    root = _root(tmp_path).resolve()
    service = WorkflowService(Storage(root))

    start_after_init(service, "automatic", "T1", agent="codex")
    _finish(service, "T1")

    assert _outputs(service, "T1") == [str(root), str(root)]


def test_project_workdir_uses_the_project_checkout_not_the_task_worktree(
    tmp_path: Path,
) -> None:
    root = _root(
        tmp_path,
        WORKFLOWS.replace(
            "workflows:\n",
            "hooks:\n  before_start_workflow:\n    - workflows: [automatic]\n"
            "      handlers:\n"
            "        - ext/ww/git/handlers:start-task-branch: ~\n"
            "        - ext/ww/git/handlers:create-worktree: ~\n"
            "workflows:\n",
            1,
        ),
    ).resolve()
    backend = root / "backend"
    for args in (
        ("init", "-q", "-b", "main", "."),
        ("config", "user.email", "t@e.st"),
        ("config", "user.name", "Test"),
        ("config", "commit.gpgsign", "false"),
        ("commit", "-q", "--allow-empty", "-m", "seed"),
    ):
        subprocess.run(("git", *args), cwd=backend, check=True, capture_output=True)
    (root / "ww.json").write_text(
        json.dumps(
            {
                "projects": [{"name": "backend", "path": "./backend"}],
                "extensions": {
                    "ww/git": {
                        "separate_branch": True,
                        "base_branches": {"default": "main"},
                        "branch_name_formats": {"default": "feature/{{ww.task.id}}"},
                        "worktrees": True,
                        "worktree_dir": "./trees",
                        "worktree_name_format": "{{ww.task.id}}",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    service = WorkflowService(Storage(root))

    start_after_init(service, "automatic", "T1", agent="codex", project="backend")
    _finish(service, "T1")

    run, run_in_task = _outputs(service, "T1")
    assert run == str(backend)
    assert run_in_task != str(backend)
    assert "trees" in Path(run_in_task).parts


def test_workdir_is_omitted_from_a_plan_when_it_is_the_default(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    service = WorkflowService(Storage(root))

    start_after_init(service, "automatic", "T1", agent="codex")

    snapshot = service.tasks.read_plan_snapshot("T1", "01-automatic")
    assert snapshot is not None
    items = {item["name"]: item for item in snapshot.plan.to_dict()["items"]}
    assert items["run"]["workdir"] == "project"
    assert "workdir" not in items["run-in-task"]


@pytest.mark.parametrize(
    "entry",
    [
        "      - work: Work.\n        workdir: home\n",
        "      - work: Work.\n        hooks:\n"
        "          after_complete:\n            - name: x\n"
        "              argv: [pwd]\n              workdir: true\n",
    ],
)
def test_an_unknown_workdir_is_rejected(tmp_path: Path, entry: str) -> None:
    root = _root(tmp_path, "workflows:\n  - name: task\n    steps:\n" + entry)
    service = WorkflowService(Storage(root))

    with pytest.raises(ConfigurationError, match="workdir must be one of"):
        service.start("task", "T1", agent="codex")


def _plan_items(service: WorkflowService, task_id: str, workflow: str) -> list:
    snapshot = service.tasks.read_plan_snapshot(task_id, f"01-{workflow}")
    assert snapshot is not None
    return [item for item in snapshot.plan.items if item.phase == "step"][1:]


def test_an_extension_handler_entry_may_choose_its_workdir(tmp_path: Path) -> None:
    root = _root(
        tmp_path,
        "workflows:\n  - name: task\n    steps:\n"
        "      - work: Work.\n        hooks:\n          after_complete:\n"
        "            - ext/ww/git/handlers:git-commit: ~\n"
        "            - ext/ww/git/handlers:git-commit: ~\n"
        "              workdir: root\n"
        "      - ext/ww/git/handlers:git-commit: ~\n"
        "        workdir: project\n",
    )
    service = WorkflowService(Storage(root))

    start_after_init(service, "task", "T1", agent="codex", project="backend")

    snapshot = service.tasks.read_plan_snapshot("T1", "01-task")
    assert snapshot is not None
    assert [
        item.workdir for item in snapshot.plan.items if item.kind == "extension"
    ] == ["task", "root", "project"]


def test_an_extension_handler_entry_carries_nothing_but_its_workdir(
    tmp_path: Path,
) -> None:
    root = _root(
        tmp_path,
        "workflows:\n  - name: task\n    steps:\n"
        "      - ext/ww/git/handlers:git-commit: ~\n"
        "        argv: [pwd]\n",
    )
    service = WorkflowService(Storage(root))

    with pytest.raises(ConfigurationError, match="normalized name"):
        service.start("task", "T1", agent="codex")
