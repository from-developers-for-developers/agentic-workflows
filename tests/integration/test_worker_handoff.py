# SPDX-License-Identifier: GPL-3.0-or-later
"""Manager dispatch and worker assignment-continuation behavior."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.workflow_helpers import assignment_token
from ww.errors import StateError
from ww.items import WorkItem
from ww.output_adapters.json_adapter import JsonOutputAdapter
from ww.output_adapters.markdown import MarkdownOutputAdapter
from ww.service import WorkflowService
from ww.storage import Storage


def _service(root: Path, runtime: str = "single") -> WorkflowService:
    (root / "ww-agentic-workflows.yaml").write_text(
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
        caller_role="worker", assignment=assignment_token(service, "TASK-1"),
        summary_for_next="Done.",
    )
    assert work.item_name == "work"
    assert work.item_status == "in_progress"
    documented = service.complete(
        "TASK-1",
        artifact="implemented",
        caller_role="worker", assignment=assignment_token(service, "TASK-1"),
        summary_for_next="Done.",
    )
    assert (tmp_path / "completed-hook.txt").exists()
    assert documented.item_name == "finish-agent"
    assert documented.item_status == "in_progress"

    handoff = service.complete(
        "TASK-1",
        artifact="documented",
        caller_role="worker", assignment=assignment_token(service, "TASK-1"),
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
    (tmp_path / "ww-agentic-workflows.yaml").write_text(
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
    (tmp_path / "ww-agentic-workflows.yaml").write_text(
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


def test_materialized_item_and_successor_run_are_manager_boundaries(
    tmp_path: Path,
) -> None:
    (tmp_path / "ww-agentic-workflows.yaml").write_text(
        """workflows:
  - name: items
    steps:
      - name: collect
        items:
          steps:
            - name: process
              item_phase: analyze
              hooks:
                before_start:
                  - argv: [touch, materialized-preparation.txt]
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
    assert not (tmp_path / "materialized-preparation.txt").exists()
    process = service.next("TASK-I", caller_role="manager")
    assert (tmp_path / "materialized-preparation.txt").exists()
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
    (tmp_path / "ww-agentic-workflows.yaml").write_text(
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
    (tmp_path / "ww-agentic-workflows.yaml").write_text(
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
    (tmp_path / "ww-agentic-workflows.yaml").write_text(
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
    (tmp_path / "ww-agentic-workflows.yaml").write_text(
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
        hooks:
          after_complete:
            - name: commit
        loop:
          - name: review
            break: Clean.
          - name: fix
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

    # The break leaves only the wrapper's commit hook, which wants a value.
    boundary = service.loop(
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
    (tmp_path / "ww-agentic-workflows.yaml").write_text(
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
    (tmp_path / "ww-agentic-workflows.yaml").write_text(
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
    (tmp_path / "ww-agentic-workflows.yaml").write_text(
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
    (tmp_path / "ww-agentic-workflows.yaml").write_text(
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
        hooks:
          after_complete:
            - name: commit
            - name: notify
        loop:
          - name: review
            break: Clean.
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
    service.loop(
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
