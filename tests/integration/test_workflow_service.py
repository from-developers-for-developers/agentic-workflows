# SPDX-License-Identifier: GPL-3.0-or-later
from dataclasses import replace
from pathlib import Path

import pytest

from ww.actions import DefinedAction, Prompt
from ww.errors import StateError
from ww.output_adapters.markdown import MarkdownOutputAdapter
from ww.service import WorkflowService
from ww.storage import Storage
from ww.workflow_config import StepDefinition, WorkflowConfiguration, WorkflowDefinition


def _start_after_init(
    service: WorkflowService,
    workflow: str,
    task_id: str | None,
    **kwargs: object,
):
    instruction = service.start(workflow, task_id, **kwargs)
    if instruction.item_name == "init" and instruction.item_status == "pending":
        service.next(instruction.task_id)
        return service.complete(
            instruction.task_id,
            artifact="Recorded requirements.",
            summary_for_next="Done.",
        )
    return instruction


def test_execution_service_reports_missing_configuration(
    tmp_path: Path,
) -> None:
    service = WorkflowService(Storage(tmp_path))

    with pytest.raises(Exception, match="workflow configuration not found"):
        service.start("task", "TASK-1", agent="codex", model="gpt", reasoning="high")


def test_core_workspace_dir_resolves_to_the_canonical_project_root(
    tmp_path: Path,
) -> None:
    (tmp_path / "ww.yaml").write_text(
        """workflows:
  - task: ~
    steps:
      - work: Work in {{ww.task.workspace_dir}}.
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))

    _start_after_init(service, "task", "TASK-1", agent="codex")
    instruction = service.next("TASK-1")

    assert instruction.action_text == f"Work in {tmp_path.resolve()}."


def test_in_progress_instruction_includes_resolved_profile(tmp_path: Path) -> None:
    (tmp_path / "ww.yaml").write_text(
        """profiles:
  researcher: Investigate the problem independently.
workflows:
  - name: task
    steps:
      - name: investigate
        profile: researcher
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))
    _start_after_init(service, "task", "TASK-1", agent="codex")

    instruction = service.next("TASK-1")

    assert instruction.profile_instruction == ("Investigate the problem independently.")
    assert "### Profile" in MarkdownOutputAdapter().render_instruction(instruction)


def test_execution_selection_is_persisted_and_rendered(tmp_path: Path) -> None:
    (tmp_path / "ww.yaml").write_text(
        """workflows:
  - name: task
    steps:
      - name: work
        model: configured-model
        reasoning: high
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))
    ready = _start_after_init(
        service,
        "task",
        "TASK-1",
        agent="codex",
        model="manager-model",
        reasoning="medium",
        workflow_runtime="auto",
    )

    assert ready.workflow_runtime == "auto"
    assert ready.model == "configured-model"
    active = service.next("TASK-1", model="worker-model", reasoning="high")
    state = service.tasks.read_execution_state("TASK-1", "01-task")

    assert state is not None
    assert state.workflow_runtime == "auto"
    assert state.model == "manager-model"
    assert state.item_executions[1].model == "worker-model"
    assert active.model == "worker-model"
    rendered = MarkdownOutputAdapter().render_instruction(active)
    assert "## Worker: perform `work`" in rendered
    assert "You are the worker for this assignment" in rendered
    assert "--role worker" in rendered


def test_artifact_from_is_visible_with_an_explicit_prompt(tmp_path: Path) -> None:
    (tmp_path / "ww.yaml").write_text(
        """workflows:
  - name: task
    steps:
      - name: research
      - name: implement
        description: Implement the change.
        artifact_from: research
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))
    _start_after_init(service, "task", "TASK-1", agent="codex")
    service.next("TASK-1")
    service.complete("TASK-1", artifact="research result", summary_for_next="Done.")

    instruction = service.next("TASK-1")

    assert "Implement the change." in (instruction.action_text or "")
    assert "artifact produced by the `research` step" in (instruction.action_text or "")


def test_nested_artifact_from_names_the_upper_level_step_path(tmp_path: Path) -> None:
    (tmp_path / "ww.yaml").write_text(
        """workflows:
  - name: task
    steps:
      - name: group
        steps:
          - name: research
          - name: review
            loop:
              - name: fix
                artifact_from: research
                break: Done
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))
    _start_after_init(service, "task", "TASK-1", agent="codex")
    service.next("TASK-1")
    service.complete("TASK-1", artifact="research result", summary_for_next="Done.")

    instruction = service.next("TASK-1")
    while instruction.item_name != "fix":
        instruction = service.next("TASK-1")

    assert "artifact produced by the `group/research` step" in (
        instruction.action_text or ""
    )


def test_in_progress_instruction_lists_later_sibling_steps(tmp_path: Path) -> None:
    (tmp_path / "ww.yaml").write_text(
        """workflows:
  - name: task
    steps:
      - name: develop
      - name: run-tests
      - name: check-code-quality
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))
    _start_after_init(service, "task", "TASK-1", agent="codex")

    instruction = service.next("TASK-1")

    assert instruction.next_steps == ("run-tests", "check-code-quality")
    assert instruction.to_dict()["next_steps"] == [
        "run-tests",
        "check-code-quality",
    ]


def test_service_start_accepts_an_injected_normalized_configuration_loader(
    tmp_path: Path,
) -> None:
    configuration = WorkflowConfiguration(
        modes=(),
        profiles=(),
        handlers=(),
        global_hooks=(),
        workflows=(
            WorkflowDefinition(
                name="object-notation",
                steps=(
                    StepDefinition(
                        name="work",
                        action=DefinedAction("prompt", Prompt("Do the work.")),
                    ),
                ),
            ),
        ),
    )
    service = WorkflowService(
        Storage(tmp_path), configuration_loader=lambda: configuration
    )

    instruction = _start_after_init(service, "object-notation", "TASK-1", agent="codex")

    assert instruction.item_name == "work"
    assert not (tmp_path / "ww.yaml").exists()


def test_artifact_enabled_step_requires_a_nonempty_artifact(tmp_path: Path) -> None:
    (tmp_path / "ww.yaml").write_text(
        """workflows:
  - name: task
    steps:
      - name: work
        description: Do work.
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))
    _start_after_init(service, "task", "TASK-1", agent="codex")
    service.next("TASK-1")

    with pytest.raises(StateError, match="artifact is required"):
        service.complete("TASK-1", summary_for_next="Done.")


def test_task_workspace_instruction_explicitly_changes_directory(
    tmp_path: Path,
) -> None:
    (tmp_path / "ww.yaml").write_text(
        """workflows:
  - name: task
    steps:
      - name: work
        description: Do work.
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))
    _start_after_init(service, "task", "TASK-1", agent="codex")
    instruction = replace(
        service.next("TASK-1"), working_directory="/tmp/task worktree"
    )

    rendered = MarkdownOutputAdapter().render_instruction(instruction)

    assert "switch to this task workspace" in rendered
    assert "switch to this worktree" not in rendered
    assert "cd '/tmp/task worktree'" in rendered


def test_in_progress_instruction_includes_mcp_connection(tmp_path: Path) -> None:
    (tmp_path / "ww.yaml").write_text(
        """workflows:
  - name: task
    steps:
      - name: create-pr
        mcp: github
        description: Create a pull request for this branch.
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))
    _start_after_init(service, "task", "TASK-1", agent="codex")

    instruction = service.next("TASK-1")

    assert instruction.action_kind == "mcp"
    assert instruction.action_text == (
        "For the following work use `github` mcp connection:\n"
        "Create a pull request for this branch.\n\n"
        "If your result from the MCP call is erroneous, do not proceed to "
        "the next step. Use the `fail` command to register that and notify "
        "the caller of this task."
    )


def test_agent_can_fail_mcp_item_and_next_retries_it(tmp_path: Path) -> None:
    (tmp_path / "ww.yaml").write_text(
        """workflows:
  - name: task
    steps:
      - name: create-pr
        mcp: github
        description: Create a pull request for this branch.
      - name: notify
        description: Report the PR URL.
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))
    _start_after_init(service, "task", "TASK-1", agent="codex")
    first = service.next("TASK-1")

    failed = service.fail("TASK-1", "GitHub MCP returned a validation error.")
    retried = service.next("TASK-1")

    assert first.item_name == "create-pr"
    assert failed.status == "failed"
    assert "GitHub MCP returned a validation error." in (failed.error or "")
    assert retried.item_name == "create-pr"
    assert retried.item_status == "in_progress"


def test_force_is_required_to_skip_failed_agent_item(tmp_path: Path) -> None:
    (tmp_path / "ww.yaml").write_text(
        """workflows:
  - name: task
    steps:
      - name: create-pr
        mcp: github
        description: Create a pull request for this branch.
      - name: notify
        description: Report the PR URL.
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))
    _start_after_init(service, "task", "TASK-1", agent="codex")
    service.next("TASK-1")
    service.fail("TASK-1", "GitHub MCP returned a validation error.")

    visible = service.complete(
        "TASK-1",
        artifact="pretend success",
        summary_for_next="Done.",
    )
    assert visible.status == "failed"
    assert "GitHub MCP returned a validation error." in (visible.error or "")

    forced = service.next("TASK-1", force=True, force_reason="Operator resolved it")
    assert forced.item_name == "notify"
    assert forced.item_status == "in_progress"


def test_start_uses_the_workflow_runtime_unless_the_flag_says_otherwise(
    tmp_path: Path,
) -> None:
    (tmp_path / "ww.json").write_text(
        '{"runtime": "auto", "extensions": {}}', encoding="utf-8"
    )
    (tmp_path / "ww.yaml").write_text(
        """workflows:
  - manual: Manual testing.
    runtime: single
    steps:
      - test: Test it.
  - task: ~
    steps:
      - work: Work.
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))

    assert service.start("manual", "M-1", agent="codex").workflow_runtime == "single"
    assert service.start("task", "T-1", agent="codex").workflow_runtime == "auto"
    assert (
        service.start(
            "manual", "M-2", agent="codex", workflow_runtime="auto"
        ).workflow_runtime
        == "auto"
    )
