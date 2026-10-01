# SPDX-License-Identifier: GPL-3.0-or-later
from pathlib import Path

import pytest

from tests.workflow_helpers import assignment_token
from ww.config import load_configuration
from ww.errors import ConfigurationError
from ww.output import render_plan
from ww.output_adapters.markdown import MarkdownOutputAdapter
from ww.plan import compile_workflow_plan
from ww.project_config import ProjectConfig, load_project_config
from ww.service import WorkflowService
from ww.storage import Storage


def _plan(tmp_path: Path, yaml: str, project: ProjectConfig | None = None):
    path = tmp_path / "ww.yaml"
    path.write_text(yaml, encoding="utf-8")
    return compile_workflow_plan(
        load_configuration(path),
        tmp_path,
        "task",
        "custom:discovery-agent",
        project_config=project,
    )


def test_nested_and_referenced_execution_hints_overlay_in_order(tmp_path: Path) -> None:
    plan = _plan(
        tmp_path,
        """
handlers:
  - name: research
    description: Research.
    model: astra
    reasoning: high
workflows:
profiles:
  developer: Prefer small, well-tested changes.

workflows:
  - name: task
    agent: codex
    model: luna
    reasoning: medium
    steps:
      - name: group
        model: astra
        steps:
          - name: work
            description: Work.
            hooks:
              before_complete:
                - name: research
                  model: luna
""",
    )

    work = next(item for item in plan.items if item.name == "work")
    hook = next(item for item in plan.items if item.name == "research")
    assert (work.requested_agent, work.requested_model, work.requested_reasoning) == (
        "codex",
        "astra",
        "auto",
    )
    assert (hook.requested_model, hook.requested_reasoning) == ("luna", "auto")


def test_repeated_model_preserves_reasoning_and_auto_stops_it(tmp_path: Path) -> None:
    plan = _plan(
        tmp_path,
        """
profiles:
  developer: Prefer small, well-tested changes.

workflows:
  - name: task
    model: astra
    reasoning: high
    steps:
      - name: same
        description: Same.
        model: astra
      - name: automatic
        description: Automatic.
        model: auto
""",
    )
    same = next(item for item in plan.items if item.name == "same")
    automatic = next(item for item in plan.items if item.name == "automatic")
    assert (same.requested_model, same.requested_reasoning) == ("astra", "high")
    assert (automatic.requested_model, automatic.requested_reasoning) == (
        "auto",
        "auto",
    )


def test_role_manager_removes_step_execution_hints_and_profile(
    tmp_path: Path,
) -> None:
    plan = _plan(
        tmp_path,
        """
profiles:
  reviewer: Review carefully.
workflows:
  - name: task
    agent: workflow-agent
    model: workflow-model
    reasoning: high
    profile: reviewer
    steps:
      - name: local-work
        description: Do this locally.
        role: manager
        agent: step-agent
        model: step-model
        reasoning: medium
        profile: reviewer
""",
    )

    work = next(item for item in plan.items if item.name == "local-work")
    assert work.role == "manager"
    assert (
        work.requested_agent,
        work.requested_model,
        work.requested_reasoning,
        work.profile,
        work.profile_instruction,
    ) == (None, None, None, None, None)


def test_builtins_use_independent_defaults_without_affecting_hooks(
    tmp_path: Path,
) -> None:
    plan = _plan(
        tmp_path,
        """
hooks:
  before_start:
    - name: prepare
      description: Prepare.
workflows:
  - name: task
    model: workflow-model
    reasoning: high
    steps:
      - name: work
        profile: developer
        description: Work.
""",
        ProjectConfig(builtins={"init": {"model": "fast"}}),
    )
    init = next(item for item in plan.items if item.name == "init")
    init_hook = next(
        item for item in plan.items if item.name == "prepare" and item.step == "init"
    )
    summary = next(item for item in plan.items if item.summary)
    assert (init.requested_agent, init.requested_model, init.requested_reasoning) == (
        "custom:discovery-agent",
        "fast",
        "low",
    )
    assert (init_hook.requested_model, init_hook.requested_reasoning) == (
        "workflow-model",
        "high",
    )
    assert (summary.requested_model, summary.requested_reasoning) == ("auto", "auto")


def test_project_builtin_schema_is_strict(tmp_path: Path) -> None:
    path = tmp_path / "ww.json"
    path.write_text('{"builtins":{"unknown":{"model":"x"}}}', encoding="utf-8")
    with pytest.raises(ConfigurationError, match="unknown name"):
        load_project_config(path)


def test_agent_auto_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "ww.yaml"
    path.write_text(
        "workflows:\n- name: task\n  agent: auto\n  steps: []\n", encoding="utf-8"
    )
    with pytest.raises(ConfigurationError, match="must not be 'auto'"):
        load_configuration(path)


def test_orchestrated_preview_and_item_selection_are_separate(tmp_path: Path) -> None:
    (tmp_path / "ww.yaml").write_text(
        """
profiles:
  developer: Prefer small, well-tested changes.

workflows:
  - name: task
    model: requested-model
    reasoning: high
    profile: developer
    steps:
      - name: work
        profile: developer
        description: Work.
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))
    preview = service.start(
        "task",
        "TASK-1",
        agent="codex",
        workflow_runtime="auto",
        caller_role="manager",
    )
    assert preview.assignment_preview is not None
    assert preview.assignment_preview["requested_model"] == "requested-model"

    service.next(
        "TASK-1",
        selected_agent="worker-a",
        model="selected-model",
        reasoning="medium",
        caller_role="manager",
    )
    service.complete(
        "TASK-1",
        artifact="done",
        selected_agent="delegate",
        selected_model="delegate-model",
        selected_reasoning="low",
        caller_role="worker", assignment=assignment_token(service, "TASK-1"),
        summary_for_next="Done.",
    )
    state = service.tasks.read_execution_state("TASK-1", "01-task")
    snapshot = service.tasks.read_plan_snapshot("TASK-1", "01-task")
    assert state is not None and snapshot is not None
    index = next(i for i, item in enumerate(snapshot.plan.items) if item.name == "work")
    item = snapshot.plan.items[index]
    record = state.item_executions[index]
    assert (item.requested_model, item.requested_reasoning) == (
        "requested-model",
        "high",
    )
    assert (
        record.selected_agent,
        record.selected_model,
        record.selected_reasoning,
    ) == (
        "delegate",
        "delegate-model",
        "low",
    )


def test_unrequested_model_and_reasoning_are_not_shown(tmp_path: Path) -> None:
    """An open ``auto`` choice invites an agent to make one, so it is omitted."""
    yaml = """
workflows:
  - name: task
    steps:
      - work: Work.
      - review: Review.
        reasoning: high
"""
    plan_text = render_plan(_plan(tmp_path, yaml), json_output=False)
    assert "`auto`" not in plan_text
    assert "- Reasoning: `high`" in plan_text

    md = MarkdownOutputAdapter()
    service = WorkflowService(Storage(tmp_path))
    preview = md.render_instruction(
        service.start(
            "task",
            "TASK-1",
            agent="codex",
            workflow_runtime="auto",
            caller_role="manager",
        )
    )
    assert "Requested agent: `codex`" in preview
    assert "Requested model" not in preview
    assert "Requested reasoning" not in preview
    assert "--model" not in preview
    assert "--reasoning" not in preview

    service.next("TASK-1", selected_agent="codex", caller_role="manager")
    worker = md.render_instruction(
        service.status(
            "TASK-1",
            caller_role="worker",
            assignment=assignment_token(service, "TASK-1"),
        )
    )
    assert "`auto`" not in worker
    assert "- Model:" not in worker

    service.complete(
        "TASK-1",
        artifact="done",
        caller_role="worker",
        assignment=assignment_token(service, "TASK-1"),
        summary_for_next="Done.",
    )
    reviewing = md.render_instruction(service.status("TASK-1", caller_role="manager"))
    assert "Requested reasoning: `high`" in reviewing
    assert "--reasoning high" in reviewing
    assert "Requested model" not in reviewing
    assert "--model" not in reviewing


def test_profile_is_inherited_through_enclosing_steps(tmp_path: Path) -> None:
    plan = _plan(
        tmp_path,
        """
profiles:
  developer: Prefer small changes.
  reviewer: Look for regressions.
  tester: Run and read the tests.

workflows:
  - name: task
    profile: developer
    steps:
      - name: run-tests
        profile: tester
        loop:
          - name: test
            break: Green.
          - name: fix
          - name: review
            profile: reviewer
          - name: local
            role: manager
      - name: group
        steps:
          - name: leaf
""",
    )
    by_name = {item.name: item for item in plan.items if item.owner == "agent"}

    assert by_name["init"].profile is None
    assert (by_name["test"].profile, by_name["test"].profile_instruction) == (
        "tester",
        "Run and read the tests.",
    )
    assert by_name["fix"].profile == "tester"
    assert by_name["review"].profile == "reviewer"
    assert by_name["local"].profile is None
    assert by_name["leaf"].profile == "developer"
