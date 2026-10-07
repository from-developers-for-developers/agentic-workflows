# SPDX-License-Identifier: GPL-3.0-or-later
"""Children that obtain their own external task ID through an identity request."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.workflow_helpers import start_after_init
from ww.cli import main
from ww.errors import StateError
from ww.output_adapters.markdown import MarkdownOutputAdapter
from ww.service import WorkflowService
from ww.storage import Storage
from ww.task_ids import is_bootstrap_request

WORKFLOWS = """workflows:
  - name: epic
    steps:
      - split: Split the epic into stories.
        children:
          workflow: story
  - name: story
    steps:
      - create-story: Create the Jira story through the jira MCP connection.
        variables:
          - name: task_id
            description: The Jira key returned by the tracker.
      - implement: Implement the story.
  - name: plain-parent
    steps:
      - split: Split the work.
        children:
          workflow: plain
  - name: plain
    steps:
      - implement: Implement it.
"""


def _service(tmp_path: Path) -> WorkflowService:
    (tmp_path / "backend").mkdir(parents=True)
    (tmp_path / "ww.yaml").write_text(WORKFLOWS, encoding="utf-8")
    (tmp_path / "ww.json").write_text(
        json.dumps({"projects": [{"name": "backend", "path": "./backend"}]}),
        encoding="utf-8",
    )
    return WorkflowService(Storage(tmp_path))


def _split(service: WorkflowService, workflow: str = "epic") -> None:
    start_after_init(service, workflow, "EPIC-1", agent="codex")
    service.next("EPIC-1")


def test_collection_step_explains_that_children_bind_their_own_ids(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    _split(service)

    text = service.status("EPIC-1").action_text or ""

    assert './ww add-child EPIC-1 --text="<child task text>"' in text
    assert "--id <child-id>" not in text
    assert "Do not pass `--id`" in text
    plain = _service(tmp_path / "other")
    _split(plain, "plain-parent")
    assert "--id <child-id>" in (plain.status("EPIC-1").action_text or "")


def test_a_child_added_without_an_id_is_named_by_its_request(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _split(service)

    child = service.add_child("EPIC-1", None, "Story one", project="backend")
    explicit = service.add_child("EPIC-1", "PROJ-9", "Story with a known key")

    assert is_bootstrap_request(child.id)
    assert child.task_id == f"EPIC-1/{child.id}"
    assert explicit.id == "PROJ-9"
    # A plain child workflow keeps generating ordinary IDs.
    plain = _service(tmp_path / "other")
    _split(plain, "plain-parent")
    assert not is_bootstrap_request(plain.add_child("EPIC-1", None, "Part").id)


def test_the_child_binds_its_id_and_the_parent_record_is_renamed(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    md = MarkdownOutputAdapter()
    _split(service)
    child = service.add_child("EPIC-1", None, "Story one", project="backend")
    service.complete("EPIC-1", artifact="split", summary_for_next="Done.")
    waiting = service.next("EPIC-1")
    assert f"./ww start-child EPIC-1 {child.id}" in (waiting.action_text or "")

    request = service.start_child("EPIC-1", child.id)
    assert (request.task_id, request.workflow, request.item_name) == (
        child.id,
        "story",
        "create-story",
    )
    assert request.status == "pending"
    parent_text = service.status("EPIC-1").action_text or ""
    assert f"Child `{child.id}` is obtaining its task ID." in parent_text
    assert f"./ww instruction {child.id} --role manager" in parent_text
    active = service.next(child.id)
    assert active.item_status == "in_progress"
    assert [value.name for value in active.required_values] == ["task_id"]
    assert f"./ww complete {child.id} --role worker" in md.render_instruction(active)

    bound = service.complete(
        child.id,
        variables=(("task_id", "PROJ-456"),),
        artifact="Created PROJ-456.",
        summary_for_next="Done.",
    )

    assert (bound.task_id, bound.workflow, bound.item_name) == (
        "EPIC-1/PROJ-456",
        "story",
        "implement",
    )
    children = service.tasks.read_children("EPIC-1", "01-epic")
    assert [(c.id, c.task_id, c.status, c.project) for c in children] == [
        ("PROJ-456", "EPIC-1/PROJ-456", "in_progress", "backend")
    ]
    state = service.tasks.read_execution_state("EPIC-1/PROJ-456", "01-story")
    assert state is not None
    assert state.parent_task_id == "EPIC-1"
    assert state.start_operation_id == children[0].start_operation_id
    assert state.working_directory == "backend"
    assert dict(state.workflow_values)["task_id"] == "EPIC-1/PROJ-456"
    snapshot = service.tasks.read_plan_snapshot("EPIC-1/PROJ-456", "01-story")
    assert snapshot is not None
    assert [item.name for item in snapshot.plan.items] == [
        "init",
        "implement",
        "update-workflow-summary",
    ]

    # Binding again, as a retried request would, changes nothing.
    service.children.bind_child("EPIC-1", child.id, "EPIC-1/PROJ-456")
    with pytest.raises(StateError, match="already completed"):
        service.next(child.id)

    service.next("EPIC-1/PROJ-456")
    service.complete("EPIC-1/PROJ-456", artifact="done", summary_for_next="Done.")
    service.next("EPIC-1/PROJ-456")
    service.complete(
        "EPIC-1/PROJ-456",
        variables=(("summary", "Story done"),),
        summary_for_next="Done.",
    )
    finished = service.status("EPIC-1")
    assert finished.status == "completed"
    assert [(c.id, c.status, c.summary) for c in finished.child_tasks or ()] in (
        [],
        [("PROJ-456", "completed", "Story done")],
    )
    assert [
        (c.id, c.status, c.summary)
        for c in service.tasks.read_children("EPIC-1", "01-epic")
    ] == [("PROJ-456", "completed", "Story done")]


def test_a_bound_id_must_be_a_free_single_segment(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _split(service)
    first = service.add_child("EPIC-1", None, "Story one")
    service.add_child("EPIC-1", "PROJ-1", "Known story")
    service.complete("EPIC-1", artifact="split", summary_for_next="Done.")
    service.next("EPIC-1")
    service.start_child("EPIC-1", first.id)
    service.next(first.id)

    with pytest.raises(StateError, match="child ID must be a single normalized name"):
        service.complete(
            first.id,
            variables=(("task_id", "a/b"),),
            summary_for_next="Done.",
        )
    with pytest.raises(StateError, match="already exists"):
        service.complete(
            first.id,
            variables=(("task_id", "PROJ-1"),),
            summary_for_next="Done.",
        )
    assert service.status(first.id).item_status == "in_progress"


def test_the_identity_flow_works_through_the_cli(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    service = _service(tmp_path)
    _split(service)
    root = str(tmp_path)

    assert main(["--root", root, "add-child", "EPIC-1", "--text", "S1"]) == 0
    child_id = json.loads(capsys.readouterr().out)["id"]
    assert is_bootstrap_request(child_id)
    service.complete("EPIC-1", artifact="split", summary_for_next="Done.")
    service.next("EPIC-1")

    assert main(["--root", root, "start-child", "EPIC-1", child_id, "--json"]) == 0
    started = json.loads(capsys.readouterr().out)
    assert (started["task_id"], started["item_name"]) == (child_id, "create-story")
    assert main(["--root", root, "next", child_id, "--role", "manager"]) == 0
    capsys.readouterr()
    assert (
        main(
            [
                "--root",
                root,
                "complete",
                child_id,
                "--role",
                "worker",
                "--variable",
                "task_id=PROJ-7",
                "--artifact",
                "Created PROJ-7.",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "# EPIC-1/PROJ-7 · story" in output
    assert main(["--root", root, "status", "EPIC-1/PROJ-7"]) == 0
    assert "implement" in capsys.readouterr().out


def _open_request(service: WorkflowService) -> str:
    """Add one child, finish the split, and open the child's identity request."""
    _split(service)
    child = service.add_child("EPIC-1", None, "Story one", project="backend")
    service.complete("EPIC-1", artifact="split", summary_for_next="Done.")
    service.next("EPIC-1")
    service.start_child("EPIC-1", child.id)
    return child.id


def test_the_identity_page_carries_the_child_text_as_requirements(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    request_id = _open_request(service)

    active = service.next(request_id)

    requirements = f"Requirements for child task {request_id}: Story one"
    assert active.task_requirements == requirements
    assert active.requirements_command == f"./ww requirements {request_id}"
    page = MarkdownOutputAdapter().render_instruction(active)
    assert "### Task requirements" in page
    assert requirements in page
    recorded = service.requirements(request_id)
    assert (recorded.task_id, recorded.text, recorded.amendments) == (
        request_id,
        requirements,
        (),
    )
    # The parent's child record names the request under the parent.
    assert service.requirements(f"EPIC-1/{request_id}").text == requirements
    with pytest.raises(StateError, match="do not have workflow runs"):
        service.requirements(request_id, "01-story")


def test_a_failed_request_offers_retry_and_reset_but_never_force(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    request_id = _open_request(service)
    service.next(request_id)

    failed = service.fail(f"EPIC-1/{request_id}", "jira is down")

    assert (failed.task_id, failed.status, failed.operator_reason) == (
        request_id,
        "failed",
        "work_failed",
    )
    assert [(c.action, c.command) for c in failed.recovery_commands] == [
        ("retry", f"./ww next {request_id} --retry --yes --role manager")
    ]
    page = MarkdownOutputAdapter().render_instruction(failed)
    assert "cannot be skipped" in page
    assert f"./ww next {request_id} --retry --yes --role manager" in page
    assert f"./ww reset {request_id} --yes" in page
    assert "--force" not in page
    # Without a decision the failed page is shown again, under either name.
    assert service.next(request_id).status == "failed"
    assert service.status(f"EPIC-1/{request_id}").status == "failed"
    assert service.task_status(f"EPIC-1/{request_id}").step_state == "failed"
    with pytest.raises(StateError, match="cannot be skipped"):
        service.next(request_id, force=True, force_reason="skip it")

    reopened = service.next(f"EPIC-1/{request_id}", retry=True)

    assert (reopened.task_id, reopened.item_status, reopened.error) == (
        request_id,
        "in_progress",
        None,
    )
    assert service.storage.read_bootstrap(request_id)["status"] == "in_progress"
    with pytest.raises(StateError, match="nothing to retry"):
        service.next(request_id, retry=True)


def test_reset_drops_a_request_so_start_child_opens_it_again(tmp_path: Path) -> None:
    service = _service(tmp_path)
    md = MarkdownOutputAdapter()
    request_id = _open_request(service)
    service.next(request_id)
    service.fail(request_id, "jira is down")
    request_file = tmp_path / ".ww" / "bootstrap" / f"{request_id}.json"
    assert request_file.exists()

    result = service.reset(f"EPIC-1/{request_id}")

    assert (result.task_id, result.removed, result.identity_request) == (
        request_id,
        True,
        True,
    )
    assert "opens a fresh request" in md.render_reset(result)
    assert not request_file.exists()
    children = service.tasks.read_children("EPIC-1", "01-epic")
    assert [(c.id, c.status) for c in children] == [(request_id, "pending")]
    assert children[0].start_operation_id is not None
    again = service.reset(request_id)
    assert (again.removed, again.identity_request) == (False, True)
    assert "was not found" in md.render_reset(again)

    reopened = service.start_child("EPIC-1", request_id)

    assert (reopened.task_id, reopened.status, reopened.error) == (
        request_id,
        "pending",
        None,
    )
    service.next(request_id)
    bound = service.complete(
        request_id,
        variables=(("task_id", "PROJ-3"),),
        artifact="Created PROJ-3.",
        summary_for_next="Done.",
    )
    assert bound.task_id == "EPIC-1/PROJ-3"
    with pytest.raises(StateError, match="reset that task instead"):
        service.reset(request_id)


def test_the_request_recovery_works_through_the_cli(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    service = _service(tmp_path)
    request_id = _open_request(service)
    service.next(request_id)
    root = str(tmp_path)
    qualified = f"EPIC-1/{request_id}"

    assert main(["--root", root, "requirements", qualified]) == 0
    assert "Story one" in capsys.readouterr().out
    # A page that waits for the operator exits non-zero, as any failure does.
    assert main(["--root", root, "fail", qualified, "--error", "down"]) == 1
    assert f"./ww next {request_id} --retry --yes" in capsys.readouterr().out
    assert main(["--root", root, "next", qualified, "--retry", "--yes"]) == 0
    assert f"./ww complete {request_id} --role worker" in capsys.readouterr().out
    assert main(["--root", root, "reset", qualified, "--yes"]) == 0
    assert f"Identity request {request_id} was reset" in capsys.readouterr().out
    assert main(["--root", root, "start-child", "EPIC-1", request_id]) == 0
    assert f"./ww next {request_id} --role manager" in capsys.readouterr().out
