# SPDX-License-Identifier: GPL-3.0-or-later
"""Manager dispatch and worker assignment-continuation behavior."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.workflow_helpers import assignment_token, configured_service
from ww.errors import StateError
from ww.items import WorkItem
from ww.output_adapters.json_adapter import JsonOutputAdapter
from ww.output_adapters.markdown import MarkdownOutputAdapter
from ww.service import WorkflowService
from ww.storage import Storage


def _service(root: Path, runtime: str = "single") -> WorkflowService:
    (root / "ww.yaml").write_text(
        """handlers:
  - name: prepare-agent
    description: Prepare the work.
  - name: finish-agent
    description: Document the result.
  - name: record-auto
    argv: [touch, completed-hook.txt]
workflows:
  - name: task
    steps:
      - name: work
        model: configured-worker
        reasoning: high
        description: Implement it.
        hooks:
          before_start:
            - name: prepare-agent
          before_complete:
            - name: record-auto
          after_complete:
            - name: finish-agent
      - name: verify
        description: Verify it.
        hooks:
          before_start:
            - argv: [touch, next-preparation.txt]
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(root))
    started = service.start(
        "task",
        "TASK-1",
        agent="codex",
        workflow_runtime=runtime,
        caller_role="manager",
    )
    assert started.control == "handoff_manager"
    return service


@pytest.mark.parametrize("runtime", ["single", "auto"])
def test_worker_completes_full_assignment_then_hands_back(
    tmp_path: Path, runtime: str
) -> None:
    service = _service(tmp_path, runtime)

    prepare = service.next(
        "TASK-1",
        model="worker-model",
        reasoning="medium",
        caller_role="manager",
    )
    assert prepare.item_name == "prepare-agent"
    assert prepare.control == "continue_worker"
    assert prepare.next_role == "worker"

    work = service.complete(
        "TASK-1",
        artifact="prepared",
        caller_role="worker",
        assignment=assignment_token(service, "TASK-1"),
        summary_for_next="Done.",
    )
    assert work.item_name == "work"
    assert work.item_status == "in_progress"
    documented = service.complete(
        "TASK-1",
        artifact="implemented",
        caller_role="worker",
        assignment=assignment_token(service, "TASK-1"),
        summary_for_next="Done.",
    )
    assert (tmp_path / "completed-hook.txt").exists()
    assert documented.item_name == "finish-agent"
    assert documented.item_status == "in_progress"

    handoff = service.complete(
        "TASK-1",
        artifact="documented",
        caller_role="worker",
        assignment=assignment_token(service, "TASK-1"),
        summary_for_next="Done.",
    )
    assert handoff.item_name == "inline-argv"
    assert handoff.item_status == "pending"
    assert handoff.control == "handoff_manager"
    assert handoff.next_role == "manager"
    assert handoff.continuation_command == "./ww next TASK-1 --role manager"
    assert not (tmp_path / "next-preparation.txt").exists()
    rendered_handoff = MarkdownOutputAdapter().render_instruction(handoff)
    assert "Completion recorded successfully by `ww`" in rendered_handoff
    if runtime == "auto":
        assert "## Worker: return control to the manager" in rendered_handoff
        assert "This assignment is complete." in rendered_handoff
        assert "run no further `ww` command" in rendered_handoff
    else:
        assert "## Manager and worker: dispatch" in rendered_handoff
        assert "act as the manager and do the work yourself" in rendered_handoff

    reloaded = WorkflowService(Storage(tmp_path))
    status = reloaded.status(
        "TASK-1", caller_role="worker", assignment=assignment_token(reloaded, "TASK-1")
    )
    assert status.control == "handoff_manager"
    assert status.item_status == "pending"
    assert status.manager_intro is True

    verify = reloaded.next("TASK-1", caller_role="manager")
    assert (tmp_path / "next-preparation.txt").exists()
    assert verify.item_name == "verify"
    assert verify.item_status == "in_progress"

    summary = reloaded.complete(
        "TASK-1",
        artifact="verified",
        caller_role="worker",
        assignment=assignment_token(reloaded, "TASK-1"),
        summary_for_next="Done.",
    )
    assert summary.item_name == "update-workflow-summary"
    assert summary.item_status == "in_progress"
    # The summary's inputs are the ordinary steps' handovers, never a hook's.
    assert [(item.step, item.summary) for item in summary.run_handovers] == [
        ("work", "Done."),
        ("verify", "Done."),
    ]
    assert summary.run_handovers[0].artifact.endswith("02-work.md")
    rendered_summary = MarkdownOutputAdapter().render_instruction(summary)
    assert "### Step handovers of this run" in rendered_summary
    assert "- `work`: Done. (artifact: `" in rendered_summary
    assert "nothing from other runs of this task" in rendered_summary
    done = reloaded.complete(
        "TASK-1",
        variables=(("summary", "Implemented and verified."),),
        caller_role="worker",
        assignment=assignment_token(reloaded, "TASK-1"),
        summary_for_next="Done.",
    )
    assert done.status == "completed"
    assert done.control == "handoff_manager"
    assert done.next_role == "manager"
    assert done.continuation_command is None

    state = reloaded.tasks.read_execution_state("TASK-1", "01-task")
    assert state is not None
    agent_records = [
        record
        for item, record in zip(
            reloaded.tasks.read_plan_snapshot("TASK-1", "01-task").plan.items,
            state.item_executions,
            strict=True,
        )
        if item.step == "work" and item.owner == "agent"
    ]
    assert [record.status for record in agent_records] == [
        "completed",
        "completed",
        "completed",
    ]
    assert {record.model for record in agent_records} == {"worker-model"}
    assert {record.reasoning for record in agent_records} == {"medium"}


def test_protocol_renderers_and_role_rejection_are_consistent(tmp_path: Path) -> None:
    (tmp_path / "ww.yaml").write_text(
        """workflows:
  - name: task
    steps:
      - name: work
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))
    with pytest.raises(StateError, match="manager-role command"):
        service.start("task", "TASK-1", agent="codex", caller_role="worker")
    assert not service.tasks.task_exists("TASK-1")

    service.start("task", "TASK-1", agent="codex", caller_role="manager")
    with pytest.raises(StateError, match="manager-role command"):
        service.next("TASK-1", caller_role="worker")
    pending = service.status(
        "TASK-1", caller_role="worker", assignment=assignment_token(service, "TASK-1")
    )
    data = pending.to_dict()
    markdown = MarkdownOutputAdapter().render_instruction(pending)
    rendered_json = JsonOutputAdapter().render_instruction(pending)

    assert data["control"] == "handoff_manager"
    assert data["next_role"] == "manager"
    assert '"control": "handoff_manager"' in rendered_json
    assert "## Manager and worker: dispatch the `work` assignment" in markdown
    assert "Generated by `ww`" in markdown
    assert "./ww next TASK-1 --role manager" in markdown


def test_automatic_failure_reports_that_worker_result_was_saved(
    tmp_path: Path,
) -> None:
    (tmp_path / "ww.yaml").write_text(
        """handlers:
  - name: reject
    argv: ["false"]
workflows:
  - name: task
    steps:
      - name: work
        hooks:
          before_complete:
            - name: reject
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))
    service.start("task", "TASK-1", agent="codex", caller_role="manager")
    service.next("TASK-1", caller_role="manager")

    failed = service.complete(
        "TASK-1",
        artifact="finished work",
        caller_role="worker",
        assignment=assignment_token(service, "TASK-1"),
        summary_for_next="Done.",
    )
    assert failed.status == "failed"
    assert failed.control == "awaiting_operator"
    assert failed.next_role == "operator"
    assert failed.operator_reason == "handler_failed"
    assert failed.result_saved is True
    assert failed.continuation_command is None

    reloaded = WorkflowService(Storage(tmp_path))
    visible = reloaded.status(
        "TASK-1", caller_role="worker", assignment=assignment_token(reloaded, "TASK-1")
    )
    assert visible.result_saved is True
    assert visible.control == "awaiting_operator"


def test_an_items_substep_and_successor_run_are_manager_boundaries(
    tmp_path: Path,
) -> None:
    (tmp_path / "ww.yaml").write_text(
        """workflows:
  - name: items
    steps:
      - name: collect
        items:
          steps:
            - name: process
              hooks:
                before_start:
                  - argv: [touch, substep-preparation.txt]
  - name: choose
    steps:
      - name: select
        variables:
          - name: workflow
        hooks:
          after_complete:
            - handoff_to: "{{workflow}}"
  - name: target
    steps:
      - name: target-work
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))
    service.start("items", "TASK-I", agent="codex", caller_role="manager")
    service.next("TASK-I", caller_role="manager")
    service.add_item("TASK-I", WorkItem("item-1", "First item"))

    boundary = service.complete(
        "TASK-I",
        artifact="collected",
        caller_role="worker",
        assignment=assignment_token(service, "TASK-I"),
        summary_for_next="Done.",
    )
    assert boundary.control == "handoff_manager"
    assert boundary.item_name == "inline-argv"
    assert not (tmp_path / "substep-preparation.txt").exists()
    process = service.next("TASK-I", caller_role="manager")
    assert (tmp_path / "substep-preparation.txt").exists()
    assert process.item_name == "process"

    service.start("choose", "TASK-H", agent="codex", caller_role="manager")
    selection = service.next("TASK-H", caller_role="manager")
    assert selection.item_name == "select"
    transition = service.complete(
        "TASK-H",
        variables=(("workflow", "target"),),
        artifact="selected target",
        caller_role="worker",
        assignment=assignment_token(service, "TASK-H"),
        summary_for_next="Done.",
    )
    assert transition.action_kind == "workflow_transition"
    assert transition.control == "handoff_manager"

    successor = service.next("TASK-H", caller_role="manager")
    assert successor.workflow == "target"
    assert successor.item_name == "target-work"
    assert successor.item_status == "pending"
    assert successor.control == "handoff_manager"


def test_interrupted_worker_assignment_recovers_through_manager(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "ww.yaml").write_text(
        """handlers:
  - name: publish
    argv: [touch, published.txt]
workflows:
  - name: task
    steps:
      - name: work
        hooks:
          before_complete:
            - name: publish
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))
    service.start("task", "TASK-1", agent="codex", caller_role="manager")
    service.next("TASK-1", caller_role="manager")

    def interrupted(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise KeyboardInterrupt

    monkeypatch.setattr(subprocess, "Popen", interrupted)
    with pytest.raises(KeyboardInterrupt):
        service.complete(
            "TASK-1",
            artifact="done",
            caller_role="worker",
            assignment=assignment_token(service, "TASK-1"),
            summary_for_next="Done.",
        )

    resumed = WorkflowService(Storage(tmp_path))
    uncertain = resumed.status(
        "TASK-1", caller_role="worker", assignment=assignment_token(resumed, "TASK-1")
    )
    assert uncertain.control == "blocked"
    assert uncertain.next_role == "manager"
    assert uncertain.result_saved is True

    interrupted_state = resumed.next("TASK-1", caller_role="manager")
    assert interrupted_state.status == "interrupted"
    assert interrupted_state.control == "awaiting_operator"
    assert interrupted_state.next_role == "operator"
    assert interrupted_state.operator_reason == "handler_interrupted"
    summary = resumed.recover("TASK-1", mark_succeeded=True, caller_role="manager")
    assert summary.item_name == "update-workflow-summary"
    assert summary.item_status == "in_progress"
    assert summary.control == "continue_worker"
    assert not (tmp_path / "published.txt").exists()


def test_declared_preparation_input_remains_worker_assignment_work(
    tmp_path: Path,
) -> None:
    (tmp_path / "ww.yaml").write_text(
        """handlers:
  - name: prepare
    variables:
      - name: note
    shell: printf "%s" "$NOTE" > note.txt
    env:
      NOTE: "{{note}}"
workflows:
  - name: task
    steps:
      - name: work
        hooks:
          before_start:
            - name: prepare
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))
    service.start("task", "TASK-1", agent="codex", caller_role="manager")

    request = service.next("TASK-1", caller_role="manager")
    assert request.status == "awaiting_input"
    assert request.control == "continue_worker"
    assert request.next_role == "worker"
    assert request.continuation_command == (
        './ww complete TASK-1 --role worker --variable note="<note>" '
        '--artifact="<whole result in Markdown>"'
    )

    active = service.complete(
        "TASK-1",
        variables=(("note", "prepared"),),
        caller_role="worker",
        assignment=assignment_token(service, "TASK-1"),
        summary_for_next="Done.",
    )
    assert active.item_name == "work"
    assert active.item_status == "in_progress"
    assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "prepared"


def test_child_workflow_coordination_returns_to_manager(tmp_path: Path) -> None:
    (tmp_path / "ww.yaml").write_text(
        """workflows:
  - name: parent
    steps:
      - name: split
        children:
          workflow: child
  - name: child
    steps:
      - name: work
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))
    service.start("parent", "TASK-1", agent="codex", caller_role="manager")
    service.next("TASK-1", caller_role="manager")
    service.add_child("TASK-1", "TASK-1.1", "Child work")

    boundary = service.complete(
        "TASK-1",
        artifact="children collected",
        caller_role="worker",
        assignment=assignment_token(service, "TASK-1"),
        summary_for_next="Done.",
    )
    assert boundary.item_name == "children"
    assert boundary.item_status == "pending"
    assert boundary.control == "blocked"
    assert boundary.next_role == "manager"
    assert boundary.continuation_command == "./ww next TASK-1 --role manager"

    coordination = service.next("TASK-1", caller_role="manager")
    assert coordination.action_kind == "child_workflow"
    assert coordination.control == "blocked"
    assert coordination.next_role == "manager"


def test_input_only_assignment_stays_with_the_manager(tmp_path: Path) -> None:
    (tmp_path / "ww.yaml").write_text(
        """handlers:
  - name: commit
    variables:
      - name: commit_message
    shell: printf "%s" "$MESSAGE" > commit.txt
    env:
      MESSAGE: "{{commit_message}}"
workflows:
  - name: task
    steps:
      - name: review-and-fix
        steps:
          - name: review
      - name: commit
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))
    service.start(
        "task",
        "TASK-1",
        agent="codex",
        workflow_runtime="auto",
        init_artifact="Reimplement the parser fresh; do not reuse the old commits.",
        caller_role="manager",
    )
    service.next("TASK-1", caller_role="manager")

    # Every worker reads the saved requirements and the artifact contract.
    review = service.status(
        "TASK-1", caller_role="worker", assignment=assignment_token(service, "TASK-1")
    )
    assert review.task_requirements == (
        "Reimplement the parser fresh; do not reuse the old commits."
    )
    rendered = MarkdownOutputAdapter().render_instruction(review)
    assert "### Task requirements" in rendered
    assert "Reimplement the parser fresh; do not reuse the old commits." in rendered
    assert rendered.index("### Task requirements") < rendered.index(
        "### Work instruction"
    )
    assert "write nothing under `.ww`" in rendered

    # The review leaves only the commit handler, which wants a value.
    boundary = service.complete(
        "TASK-1",
        artifact="Clean.",
        caller_role="worker",
        assignment=assignment_token(service, "TASK-1"),
        summary_for_next="Done.",
    )
    preview = boundary.assignment_preview or {}
    assert "no worker is selected" in str(preview.get("message"))
    request = service.next("TASK-1", caller_role="manager")
    assert request.status == "awaiting_input"
    assert (request.next_role, request.manager_input) == ("manager", True)
    assert request.continuation_command == (
        "./ww complete TASK-1 --role manager --variable commit_message="
        '"<commit_message>"'
    )
    assert request.assignment_preview is None
    rendered = MarkdownOutputAdapter().render_instruction(request)
    assert "## Manager: provide required input" in rendered
    assert "Do not delegate this to a worker." in rendered
    assert [(item.step, item.summary) for item in request.input_context] == [
        ("review", "Done.")
    ]
    assert "### Work these values describe" in rendered
    assert "Steps completed since `commit` last ran in this run" in rendered
    assert "- `review`: Done. (artifact: `" in rendered
    assert "### Manager command" in rendered
    assert "Worker bootstrap" not in rendered

    summary = service.complete(
        "TASK-1",
        variables=(("commit_message", "Parser rewritten"),),
        caller_role="manager",
        summary_for_next="Done.",
    )
    assert summary.item_name == "update-workflow-summary"
    assert (tmp_path / "commit.txt").read_text(encoding="utf-8") == "Parser rewritten"


def test_each_step_sees_the_previous_step_result_but_never_a_hook(
    tmp_path: Path,
) -> None:
    (tmp_path / "ww.yaml").write_text(
        """handlers:
  - name: note
    description: Leave a note.
    kind: prompt
workflows:
  - name: task
    steps:
      - name: research
        hooks:
          after_complete:
            - name: note
      - name: plan
      - name: build
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))
    service.start("task", "TASK-1", agent="codex", init_artifact="Ship it.")

    first = service.next("TASK-1")
    assert first.item_name == "research"
    assert first.previous_step is None
    assert "### Previous step result" not in MarkdownOutputAdapter().render_instruction(
        first
    )

    hook = service.complete(
        "TASK-1",
        artifact="Found three call sites.",
        summary_for_next="Three call sites found.",
    )
    assert hook.item_name == "note"
    if hook.item_status != "in_progress":
        service.next("TASK-1")
    service.complete("TASK-1", artifact="Noted.", summary_for_next="Done.")

    plan = service.next("TASK-1")
    assert plan.item_name == "plan"
    assert plan.previous_step == "research"
    assert plan.previous_step_artifact == str(
        tmp_path / ".ww/tasks/TASK-1/runs/01-task/steps/02-research.md"
    )
    rendered = MarkdownOutputAdapter().render_instruction(plan)
    assert "### Previous step result" in rendered
    assert "The `research` step left this summary for you:" in rendered
    assert "> Three call sites found." in rendered
    assert plan.previous_step_summary == "Three call sites found."
    assert "Its full result, when you need more: `" in rendered
    assert "02-research.md`" in rendered
    # The body stays in the artifact; only the handover and reference travel.
    assert "Found three call sites." not in rendered
    assert "Noted." not in rendered
    assert (
        rendered.index("### Task requirements")
        < rendered.index("### Previous step result")
        < rendered.index("### Work instruction")
    )

    service.complete(
        "TASK-1",
        artifact="Plan: touch two files.",
        summary_for_next="Done.",
    )
    build = service.next("TASK-1")
    assert (build.previous_step, build.previous_step_summary) == ("plan", "Done.")
    assert (build.previous_step_artifact or "").endswith("03-plan.md")


def test_a_step_must_hand_over_a_summary_but_hooks_need_not(tmp_path: Path) -> None:
    (tmp_path / "ww.yaml").write_text(
        """handlers:
  - name: note
    description: Leave a note.
    kind: prompt
workflows:
  - name: task
    steps:
      - name: research
        hooks:
          after_complete:
            - name: note
      - name: plan
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))
    service.start("task", "TASK-1", agent="codex", init_artifact="Ship it.")

    research = service.next("TASK-1")
    assert research.summary_required is True
    rendered = MarkdownOutputAdapter().render_instruction(research)
    assert '--summary="<one or two sentences for the next step>"' in (
        research.continuation_command or ""
    )
    assert "`--summary` is the next step's handover" in rendered

    with pytest.raises(StateError, match="needs --summary"):
        service.complete("TASK-1", artifact="Found three call sites.")
    with pytest.raises(StateError, match="needs --summary"):
        service.complete(
            "TASK-1", artifact="Found three call sites.", summary_for_next="  "
        )

    hook = service.complete(
        "TASK-1",
        artifact="Found three call sites.",
        summary_for_next="Three call sites use the old parser; see the list.",
    )
    assert hook.item_name == "note"
    assert hook.summary_required is False
    assert "--summary" not in (hook.continuation_command or "")
    if hook.item_status != "in_progress":
        service.next("TASK-1")
    service.complete("TASK-1", artifact="Noted.", summary_for_next="Done.")

    plan = service.next("TASK-1")
    assert plan.previous_step == "research"
    assert plan.previous_step_summary == (
        "Three call sites use the old parser; see the list."
    )
    rendered = MarkdownOutputAdapter().render_instruction(plan)
    assert "The `research` step left this summary for you:" in rendered
    assert "> Three call sites use the old parser; see the list." in rendered
    assert "Its full result, when you need more: `" in rendered
    assert "Found three call sites." not in rendered
    state, _ = service.load("TASK-1")
    assert state.item_executions[1].summary_for_next == (
        "Three call sites use the old parser; see the list."
    )


def test_delegate_page_describes_the_step_not_its_preparation_hook(
    tmp_path: Path,
) -> None:
    (tmp_path / "ww.yaml").write_text(
        """profiles:
  developer: Build it well.
handlers:
  - name: jira-in-progress
    description: Move the ticket to In development.
    kind: prompt
hooks:
  before_start:
    - steps: [develop]
      handlers:
        - name: jira-in-progress
workflows:
  - name: task
    steps:
      - name: develop
        profile: developer
      - name: verify
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))
    service.start(
        "task",
        "TASK-1",
        agent="codex",
        workflow_runtime="auto",
        init_artifact="Do it.",
        caller_role="manager",
    )
    md = MarkdownOutputAdapter()

    dispatched = service.next("TASK-1", caller_role="manager")
    assert dispatched.item_name == "jira-in-progress"
    assert dispatched.assignment_step == "develop"
    assert dispatched.assignment_items == ("jira-in-progress", "develop")
    assert dispatched.requested_profile == "developer"
    rendered = md.render_instruction(dispatched)
    assert "## Manager: delegate the `develop` assignment" in rendered
    assert "- Profile: `developer`" in rendered
    assert (
        "This assignment covers, in order: `jira-in-progress`, `develop`." in rendered
    )

    worker = service.status(
        "TASK-1", caller_role="worker", assignment=assignment_token(service, "TASK-1")
    )
    rendered = md.render_instruction(worker)
    assert "## Worker: perform `jira-in-progress`" in rendered
    assert "covers, in order: `jira-in-progress`, `develop`." in rendered
    assert "Build it well." in rendered
    assert worker.assignment_continues is False

    develop = service.complete(
        "TASK-1",
        artifact="Moved.",
        caller_role="worker",
        assignment=assignment_token(service, "TASK-1"),
    )
    assert (develop.item_name, develop.assignment_continues) == ("develop", True)
    rendered = md.render_instruction(develop)
    assert "Same assignment continues: next item `develop`." in rendered
    assert "Do not return to the manager yet." in rendered

    handoff = service.complete(
        "TASK-1",
        artifact="Built.",
        summary_for_next="Built it.",
        caller_role="worker",
        assignment=assignment_token(service, "TASK-1"),
    )
    assert handoff.next_role == "manager"
    assert "run no further `ww` command" in md.render_instruction(handoff)


def test_delegated_pending_input_page_has_a_delegate_heading(tmp_path: Path) -> None:
    (tmp_path / "ww.yaml").write_text(
        """handlers:
  - name: commit
    variables:
      - name: commit_message
    shell: printf "%s" "$MESSAGE" > commit.txt
    env:
      MESSAGE: "{{commit_message}}"
  - name: notify
    description: Comment on the ticket.
    kind: prompt
workflows:
  - name: task
    steps:
      - name: review-and-fix
        steps:
          - name: review
      - name: commit
        hooks:
          after_complete:
            - name: notify

""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))
    service.start(
        "task",
        "TASK-1",
        agent="codex",
        workflow_runtime="auto",
        init_artifact="Do it.",
        caller_role="manager",
    )
    service.next("TASK-1", caller_role="manager")
    service.complete(
        "TASK-1",
        artifact="Clean.",
        summary_for_next="Nothing to fix.",
        caller_role="worker",
        assignment=assignment_token(service, "TASK-1"),
    )
    md = MarkdownOutputAdapter()

    # The commit hook wants a value, but an agent-owned hook follows it in the
    # same assignment, so a worker is delegated and provides the input.
    request = service.next("TASK-1", caller_role="manager")
    assert request.status == "awaiting_input"
    assert (request.next_role, request.manager_input) == ("worker", False)
    assert request.assignment_step == "notify"
    assert request.assignment_items == ("commit", "notify", "update-workflow-summary")
    rendered = md.render_instruction(request)
    assert "## Manager: delegate the `notify` assignment" in rendered
    assert "provide required input" not in rendered
    assert "### Worker bootstrap" in rendered
    assert (
        "This assignment covers, in order: `commit`, `notify`, "
        "`update-workflow-summary`." in rendered
    )

    worker = service.status(
        "TASK-1", caller_role="worker", assignment=assignment_token(service, "TASK-1")
    )
    rendered = md.render_instruction(worker)
    assert "## Worker: provide required input" in rendered
    token = assignment_token(service, "TASK-1")
    assert (
        f"--role worker --assignment {token} "
        '--variable commit_message="<commit_message>"' in rendered
    )

    notify = service.complete(
        "TASK-1",
        variables=(("commit_message", "Reviewed"),),
        caller_role="worker",
        assignment=assignment_token(service, "TASK-1"),
    )
    assert (notify.item_name, notify.item_status) == ("notify", "in_progress")
    assert (tmp_path / "commit.txt").read_text(encoding="utf-8") == "Reviewed"


_HOOKED_STEP = """handlers:
  - name: commit
    variables:
      - name: commit_message
    shell: printf "%s" "$MESSAGE" > commit.txt
    env:
      MESSAGE: "{{commit_message}}"
  - name: git-push
    argv: [touch, pushed.txt]
  - name: notify
    description: Comment on the ticket.
    kind: prompt
workflows:
  - name: task
    steps:
      - name: develop
        hooks:
          after_complete:
            - name: commit
            - name: git-push
            - name: notify
"""


def test_coverage_line_splits_completed_from_remaining_items(tmp_path: Path) -> None:
    service = configured_service(tmp_path, _HOOKED_STEP)
    service.start(
        "task",
        "TASK-1",
        agent="codex",
        workflow_runtime="auto",
        init_artifact="Do it.",
        caller_role="manager",
    )
    md = MarkdownOutputAdapter()

    dispatched = service.next("TASK-1", caller_role="manager")
    assert dispatched.assignment_items == (
        "develop",
        "commit",
        "git-push",
        "notify",
        "update-workflow-summary",
    )
    assert dispatched.assignment_automatic_items == ("git-push",)
    assert dispatched.completed_assignment_items == ()
    assert dispatched.running_assignment_item is None
    assert (
        "This assignment covers, in order: `develop`, `commit`, `git-push` (run by "
        "`ww`), `notify`, `update-workflow-summary`. One worker performs those not "
        "run by `ww`" in md.render_instruction(dispatched)
    )

    # The commit and push hooks ran on the step's completion; a worker that
    # re-reads its page sees what is done and what it still performs.
    token = assignment_token(service, "TASK-1")
    continued = service.complete(
        "TASK-1",
        artifact="Built.",
        summary_for_next="Built it.",
        variables=(("commit_message", "Reviewed"),),
        caller_role="worker",
        assignment=token,
    )
    assert (continued.item_name, continued.item_status) == ("notify", "in_progress")
    assert continued.completed_assignment_items == ("develop", "commit", "git-push")
    rendered = md.render_instruction(
        service.status("TASK-1", caller_role="worker", assignment=token)
    )
    assert (
        "Already completed in this assignment: `develop`, `commit`, `git-push` (run "
        "by `ww`). Remaining, in order: `notify`, `update-workflow-summary`. One "
        "worker performs them all; `ww` hands each one over after the previous "
        "completion." in rendered
    )
    assert "covers, in order" not in rendered
    rendered_json = JsonOutputAdapter().render_instruction(continued)
    assert '"assignment_automatic_items": [\n    "git-push"\n  ]' in rendered_json
    assert (
        '"completed_assignment_items": [\n    "develop",\n    "commit",\n    "git-push"'
        in rendered_json
    )
    assert '"running_assignment_item": null' in rendered_json

    # One item left: nothing to enumerate.
    last = service.complete(
        "TASK-1", artifact="Commented.", caller_role="worker", assignment=token
    )
    assert last.item_name == "update-workflow-summary"
    assert last.completed_assignment_items == (
        "develop",
        "commit",
        "git-push",
        "notify",
    )
    rendered = md.render_instruction(
        service.status("TASK-1", caller_role="worker", assignment=token)
    )
    assert "Already completed" not in rendered
    assert "Remaining, in order" not in rendered
    assert "covers, in order" not in rendered


def test_page_names_the_automatic_item_ww_runs_now(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = configured_service(tmp_path, _HOOKED_STEP)
    service.start(
        "task",
        "TASK-1",
        agent="codex",
        workflow_runtime="auto",
        init_artifact="Do it.",
        caller_role="manager",
    )
    service.next("TASK-1", caller_role="manager")
    token = assignment_token(service, "TASK-1")
    popen = subprocess.Popen

    def interrupted_push(argv: list[str], *args: Any, **kwargs: Any) -> Any:
        if "pushed.txt" in argv:
            raise KeyboardInterrupt
        return popen(argv, *args, **kwargs)

    monkeypatch.setattr("ww.action_execution._PROCESS.Popen", interrupted_push)
    with pytest.raises(KeyboardInterrupt):
        service.complete(
            "TASK-1",
            artifact="Built.",
            summary_for_next="Built it.",
            variables=(("commit_message", "Reviewed"),),
            caller_role="worker",
            assignment=token,
        )

    resumed = WorkflowService(Storage(tmp_path))
    page = resumed.status("TASK-1", caller_role="worker", assignment=token)
    assert (page.item_name, page.item_status) == ("git-push", "in_progress")
    assert page.completed_assignment_items == ("develop", "commit")
    assert page.running_assignment_item == "git-push"
    assert '"running_assignment_item": "git-push"' in (
        JsonOutputAdapter().render_instruction(page)
    )


_PUSHED_STEP = """handlers:
  - name: git-push
    argv: [touch, pushed.txt]
workflows:
  - name: task
    steps:
      - name: develop
        hooks:
          after_complete:
            - name: git-push
      - name: verify
"""


def test_one_agent_item_with_an_automatic_hook_is_still_enumerated(
    tmp_path: Path,
) -> None:
    service = configured_service(tmp_path, _PUSHED_STEP)
    service.start(
        "task",
        "TASK-1",
        agent="codex",
        workflow_runtime="auto",
        init_artifact="Do it.",
        caller_role="manager",
    )
    md = MarkdownOutputAdapter()

    dispatched = service.next("TASK-1", caller_role="manager")
    assert dispatched.assignment_items == ("develop", "git-push")
    assert dispatched.assignment_automatic_items == ("git-push",)
    rendered = md.render_instruction(dispatched)
    assert "## Manager: delegate the `develop` assignment" in rendered
    assert (
        "This assignment covers, in order: `develop`, `git-push` (run by `ww`). "
        "One worker performs those not run by `ww`; `ww` hands each one over after "
        "the previous completion." in rendered
    )
    token = assignment_token(service, "TASK-1")
    worker = service.status("TASK-1", caller_role="worker", assignment=token)
    assert "covers, in order: `develop`, `git-push` (run by `ww`)." in (
        md.render_instruction(worker)
    )

    # The next assignment has nothing ww runs itself.
    handoff = service.complete(
        "TASK-1",
        artifact="Built.",
        summary_for_next="Built it.",
        caller_role="worker",
        assignment=token,
    )
    assert handoff.next_role == "manager"
    dispatched = service.next("TASK-1", caller_role="manager")
    assert dispatched.assignment_items == ("verify", "update-workflow-summary")
    assert dispatched.assignment_automatic_items == ()
    assert (
        "This assignment covers, in order: `verify`, `update-workflow-summary`. "
        "One worker performs them all;" in md.render_instruction(dispatched)
    )


def test_upcoming_assignment_preview_lists_what_it_covers(tmp_path: Path) -> None:
    service = configured_service(tmp_path, _HOOKED_STEP)
    md = MarkdownOutputAdapter()

    started = service.start(
        "task",
        "TASK-1",
        agent="codex",
        workflow_runtime="auto",
        init_artifact="Do it.",
        caller_role="manager",
    )
    preview = started.assignment_preview
    assert preview is not None
    assert preview["selection_item_name"] == "develop"
    assert preview["items"] == [
        "develop",
        "commit",
        "git-push",
        "notify",
        "update-workflow-summary",
    ]
    assert preview["automatic_items"] == ["git-push"]
    rendered = md.render_instruction(started)
    assert "### Upcoming assignment" in rendered
    assert (
        "The assignment covers, in order: `develop`, `commit`, `git-push` (run by "
        "`ww`), `notify`, `update-workflow-summary`." in rendered
    )


def test_init_named_preparation_hook_keeps_manager_worker_handoff(
    tmp_path: Path,
) -> None:
    service = configured_service(
        tmp_path,
        """handlers:
  - name: init
    description: Approve the requirements before initialization.
workflows:
  - name: task
    hooks:
      before_start:
        - steps: [init]
          name: init
    steps:
      - name: work
        artifact: false
""",
    )

    paused = service.start("task", "TASK-ROLES", caller_role="manager")
    assert paused.next_role == "manager"
    assigned = service.next("TASK-ROLES", caller_role="manager")
    assert assigned.next_role == "worker"
    handoff = service.complete(
        "TASK-ROLES",
        caller_role="worker",
        assignment=assignment_token(service, "TASK-ROLES"),
        summary_for_next="Done.",
    )
    assert handoff.item_name == "work"

    state = service.tasks.read_execution_state("TASK-ROLES", "01-task")
    assert state is not None
    assert [record.status for record in state.item_executions[:2]] == [
        "completed",
        "completed",
    ]
    assert state.pending_init_artifact is None


_CHILD_WORKFLOWS = """workflows:
  - name: parent
    steps:
      - name: split
        children:
          workflow: {child}
  - name: quick
    steps:
      - name: work
  - name: pair
    steps:
      - name: first
      - name: second
"""


def _handoff_text(tmp_path: Path, child_workflow: str) -> str:
    (tmp_path / "ww.yaml").write_text(
        _CHILD_WORKFLOWS.format(child=child_workflow), encoding="utf-8"
    )
    service = WorkflowService(Storage(tmp_path))
    service.start("parent", "TASK-1", agent="codex", caller_role="manager")
    service.next("TASK-1", caller_role="manager")
    service.add_child("TASK-1", "A", "Child work")
    service.start(
        child_workflow,
        "TASK-1/A",
        agent="codex",
        workflow_runtime="auto",
        caller_role="manager",
    )
    service.next("TASK-1/A", caller_role="manager")
    done = None
    for _ in range(6):
        # The workflow's own completion hook asks for a ``summary`` variable.
        for variables in ((), (("summary", "Child done."),)):
            try:
                done = service.complete(
                    "TASK-1/A",
                    variables=variables,
                    artifact="done",
                    caller_role="worker",
                    assignment=assignment_token(service, "TASK-1/A"),
                    summary_for_next="Done.",
                )
                break
            except StateError as error:
                if "missing required variable" not in str(error):
                    raise
        if done is not None and done.handoff_block is not None:
            break
    assert done is not None and done.handoff_block is not None
    return MarkdownOutputAdapter().render_instruction(done)


def test_a_completed_child_run_hands_back_to_the_parent(tmp_path: Path) -> None:
    text = _handoff_text(tmp_path, "quick")

    assert "Manager: continue with the parent task: `" in text
    assert "next TASK-1 --role manager" in text
    assert "next TASK-1/A" not in text


def test_a_child_mid_run_keeps_its_own_continuation(tmp_path: Path) -> None:
    text = _handoff_text(tmp_path, "pair")

    assert "next TASK-1/A --role manager" in text
    assert "parent task" not in text
