# SPDX-License-Identifier: GPL-3.0-or-later
"""End-to-end scenarios across several workflows, the git extension, and memory.

Every piece of ww state lives in memory: task runs, artifacts, command output,
metadata, the workflow configuration, project settings, and the git
extension's own records.  Only git itself touches disk, inside a temporary
repository, and each scenario proves ww never created its ``.ww`` directory.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.workflow_helpers import assignment_token
from ww.config import parse_yaml_text
from ww.extensions import ExtensionRegistry, ExtensionStore
from ww.feedback import MemoryFeedbackStore
from ww.instructions import Instruction
from ww.items import WorkItem
from ww.output_adapters.markdown import MarkdownOutputAdapter
from ww.project_config import ProjectConfig
from ww.service import WorkflowService
from ww.storage import Storage
from ww.storage_adapters import (
    MemoryProjectMetadataStorageAdapter,
    MemoryTaskStorageAdapter,
)

WORKFLOWS = """
handlers:
  - name: lint
    argv: [printf, clean]
    assert:
      - equals: clean

hooks:
  before_start_workflow:
    - workflows: [feature, triage, docs, parent, child]
      handlers:
        - ext/ww/git/handlers:is-git-clean: ~
        - ext/ww/git/handlers:start-task-branch: ~
  before_complete_workflow:
    - workflows: [docs, child]
      handlers:
        - ext/ww/git/handlers:git-commit: ~
    - workflows: [feature, docs, parent, child]
      handlers:
        - ext/ww/git/handlers:return-to-base-branch: ~

workflows:
  - name: feature
    steps:
      - develop: Implement the greeting.
        hooks:
          after_complete:
            - name: lint
      - review: Review the greeting.
        items:
          description: One item per review finding.
          steps:
            - analyze: Analyze the findings.
            - fix: Fix the findings; resolve each one.
            - reply: Report each outcome; mark each one reported.
      - polish:

        steps:
          - check: Check the greeting once more.
      - ext/ww/git/handlers:git-commit: ~

  - name: triage
    steps:
      - choose: Choose the follow-up workflow.
        variables:
          - name: workflow
            description: The workflow to run next.
        hooks:
          after_complete:
            - handoff_to: "{{workflow}}"

  - name: docs
    steps:
      - write-docs: Write the documentation.

  - name: parent
    steps:
      - split: Split the work into child tasks.
        children:
          workflow: child

  - name: child
    steps:
      - implement: Implement this child task.
"""

GIT_SETTINGS = {
    "commit_format": "{{ww.task.id}}: {{commit_message}}",
    "base_branches": {"default": "main"},
    "separate_branch": True,
    "branch_name_formats": {"default": "feature/{{ww.task.id}}"},
}


class MemoryExtensionStore(ExtensionStore):
    """An extension store whose files are strings in a dictionary."""

    def __init__(self, root: Path, identifier: str) -> None:
        self.root = Path(root)
        self.identifier = identifier
        # Only used to validate file names; nothing is ever created there.
        self.directory = self.root / ".ww" / "ext" / identifier
        self.files: dict[str, str] = {}

    def append_line(self, name: str, line: str) -> None:
        self.path(name)
        self.files[name] = self.files.get(name, "") + line + "\n"

    def write_text(self, name: str, content: str) -> None:
        self.path(name)
        self.files[name] = content

    def update_text(self, name: str, update: Callable[[str | None], str]) -> str:
        self.path(name)
        content = update(self.files.get(name))
        if not isinstance(content, str):
            raise TypeError("extension store update must return a string")
        self.files[name] = content
        return content

    def read_text(self, name: str) -> str | None:
        self.path(name)
        return self.files.get(name)


class MemoryExtensionRegistry(ExtensionRegistry):
    """Bundled extensions with in-memory project settings and stores."""

    project_config = ProjectConfig(extensions={"ww/git": GIT_SETTINGS})

    def __init__(self, root: Path, *args: object, **kwargs: object) -> None:
        super().__init__(root, *args, **kwargs)  # type: ignore[arg-type]
        self.stores: dict[str, MemoryExtensionStore] = {}

    @property
    def config(self) -> ProjectConfig:
        return self.project_config

    def store(self, identifier: str) -> MemoryExtensionStore:
        self.get(identifier)
        return self.stores.setdefault(
            identifier, MemoryExtensionStore(self.root, identifier)
        )


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ("git", *args), cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main", ".")
    _git(tmp_path, "config", "user.email", "t@e.st")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "README.md").write_text("seed\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "seed")
    return tmp_path


class Project:
    """A ww service whose every store is in memory, over one git repository."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.tasks = MemoryTaskStorageAdapter()
        self.registry = MemoryExtensionRegistry.discover(root)
        self.service = WorkflowService(
            Storage(root),
            self.tasks,
            self.registry,
            configuration_loader=lambda: parse_yaml_text(WORKFLOWS, "scenario"),
            project_metadata=MemoryProjectMetadataStorageAdapter(),
            feedback_store=MemoryFeedbackStore(Storage(root)),
        )
        self.markdown = MarkdownOutputAdapter()

    def edit(self, name: str, content: str) -> None:
        """Stand in for an agent changing project files."""
        (self.root / name).write_text(content, encoding="utf-8")

    def render(self, instruction: Instruction) -> str:
        return self.markdown.render_instruction(instruction)

    def branch(self) -> str:
        return _git(self.root, "rev-parse", "--abbrev-ref", "HEAD")

    def commits(self, branch: str) -> list[str]:
        return _git(self.root, "log", "--format=%s", branch).splitlines()

    def recorded(self, name: str) -> list[dict[str, object]]:
        store = self.registry.stores["ww/git"]
        return [json.loads(line) for line in store.read_lines(name)]

    def artifact(self, task_id: str, step: str, run_id: str | None = None) -> str:
        references = [
            entry["artifact"]
            for entry in self.service.artifacts(task_id, run_id)
            if entry.get("step") == step and "artifact" in entry
        ]
        assert len(references) == 1, references
        return self.tasks.artifacts[references[0]]

    def command_output(self, task_id: str, step: str, stream: str = "stdout") -> str:
        references = [
            entry["command_output"]
            for entry in self.service.artifacts(task_id)
            if entry.get("step") == step and entry.get("stream") == stream
        ]
        assert len(references) == 1, references
        return self.tasks.artifacts[references[0]]

    def assert_nothing_written_by_ww(self) -> None:
        assert not (self.root / ".ww").exists()
        assert _git(self.root, "status", "--porcelain") == ""


@pytest.fixture
def project(repository: Path) -> Project:
    return Project(repository)


# --------------------------------------------------------------------------- #
# Scenarios
# --------------------------------------------------------------------------- #


def run_feature(project: Project) -> None:
    """A standalone workflow with a command hook, items, and a group (auto)."""
    service, task = project.service, "TASK-1"

    started = service.start(
        "feature",
        task,
        agent="codex",
        workflow_runtime="auto",
        caller_role="manager",
        init_artifact="Add a greeting.",
    )
    assert project.branch() == "feature/task-1"
    assert (started.item_name, started.item_status) == ("develop", "pending")
    assert project.artifact(task, "init").endswith("## Result\n\nAdd a greeting.\n")

    # develop, then the command hook runs before the manager boundary.
    develop = service.next(task, caller_role="manager")
    assert develop.action_text == "Implement the greeting."
    assert "You are the worker for this assignment" in project.render(
        service.status(
            task, caller_role="worker", assignment=assignment_token(service, task)
        )
    )
    project.edit("greeting.txt", "hello\n")
    boundary = service.complete(
        task,
        artifact="Implemented.",
        caller_role="worker",
        assignment=assignment_token(service, task),
        summary_for_next="Done.",
    )
    assert (boundary.item_name, boundary.next_role) == ("review", "manager")
    assert project.command_output(task, "develop") == "clean"
    assert project.artifact(task, "develop") == (
        "# TASK-1 — develop\n\n"
        "## Workflow context\n\n"
        "- Workflow: feature\n"
        "- Step: 2 of 5\n"
        "- Skill: auto\n\n"
        "## Result\n\n"
        "Implemented.\n"
    )

    # review collects items with the splitting guidance.
    review = service.next(task, caller_role="manager")
    assert "How to split: One item per review finding." in (review.action_text or "")
    service.add_item(task, WorkItem("f1", "Greet politely."))
    service.add_item(task, WorkItem("f2", "End with a newline."))
    boundary = service.complete(
        task,
        artifact="Collected.",
        caller_role="worker",
        assignment=assignment_token(service, task),
        summary_for_next="Done.",
    )
    assert boundary.next_role == "manager"

    # Each authored substep runs once over the collection, as its own
    # assignment; the worker records the transitions under its assignment.
    for name, mark in (
        ("analyze", None),
        ("fix", service.resolve_item),
        ("reply", service.report_item),
    ):
        page = service.next(task, caller_role="manager")
        assert page.item_name == name
        token = assignment_token(service, task)
        worker = service.status(task, caller_role="worker", assignment=token)
        assert "You are working within items context `review`" in (
            worker.action_text or ""
        )
        for item_id in ("f1", "f2") if mark is not None else ():
            mark(task, item_id, caller_role="worker", assignment=token)
        after = service.complete(
            task,
            artifact=f"{name} done.",
            caller_role="worker",
            assignment=token,
            summary_for_next="Done.",
        )
        assert after.next_role == "manager"
    assert all(item.resolved and item.reported for item in service.items(task))

    first = service.next(task, caller_role="manager")
    assert first.item_name == "check"
    project.edit("greeting.txt", "Hello!\n")
    commit = service.complete(
        task,
        artifact="Nothing left.",
        caller_role="worker",
        assignment=assignment_token(service, task),
        summary_for_next="Done.",
    )
    assert commit.item_name == "git-commit"

    # The commit handler asks for its message: an input-only assignment, so
    # the manager supplies it itself instead of delegating.
    request = service.next(task, caller_role="manager")
    assert request.status == "awaiting_input"
    assert [value.name for value in request.required_values] == ["commit_message"]
    assert (request.next_role, request.manager_input) == ("manager", True)
    assert "--role manager --variable commit_message=" in (
        request.continuation_command or ""
    )
    summary = service.complete(
        task,
        variables=(("commit_message", "Add a greeting"),),
        caller_role="manager",
        summary_for_next="Done.",
    )
    assert summary.item_name == "update-workflow-summary"
    assert project.branch() == "main"
    done = service.complete(
        task,
        variables=(("summary", "Greeting added."),),
        caller_role="worker",
        assignment=assignment_token(service, task),
        summary_for_next="Done.",
    )
    assert done.status == "completed"

    assert project.commits("feature/task-1") == ["TASK-1: Add a greeting", "seed"]
    body = _git(project.root, "log", "-1", "--format=%b", "feature/task-1")
    assert body.startswith("WW-Operation: ")
    assert _git(project.root, "show", "feature/task-1:greeting.txt") == "Hello!"


def run_triage(project: Project) -> None:
    """A handoff workflow that selects and starts a successor run (single)."""
    service, task = project.service, "TASK-2"

    choose = service.start("triage", task, agent="codex", init_artifact="Explain it.")
    assert (choose.workflow, choose.item_name) == ("triage", "choose")
    assert project.branch() == "feature/task-2"
    active = service.next(task)
    assert [value.name for value in active.required_values] == ["workflow"]

    successor = service.complete(
        task,
        variables=(("workflow", "docs"),),
        artifact="Docs are needed.",
        summary_for_next="Done.",
    )
    assert (successor.workflow, successor.item_name) == ("docs", "write-docs")
    assert project.branch() == "feature/task-2"

    write = service.next(task)
    # The commit handler's input is collected with the last agent step.
    assert [value.name for value in write.required_values] == ["commit_message"]
    assert "commit_message" in (write.continuation_command or "")
    project.edit("DOCS.md", "# Greeting\n")
    summary = service.complete(
        task,
        variables=(("commit_message", "Document the greeting"),),
        artifact="Wrote the docs.",
        summary_for_next="Done.",
    )
    assert summary.item_name == "update-workflow-summary"
    service.next(task)
    service.complete(
        task,
        variables=(("summary", "Documented."),),
        summary_for_next="Done.",
    )

    runs = service.status(task).task_runs
    assert [(run.workflow, run.status, run.summary) for run in runs] == [
        ("triage", "completed", "handed off to docs"),
        ("docs", "completed", "Documented."),
    ]
    assert project.artifact(task, "choose", "01-triage").endswith("Docs are needed.\n")
    assert project.artifact(task, "write-docs", "02-docs").endswith("Wrote the docs.\n")
    assert project.commits("feature/task-2") == [
        "TASK-2: Document the greeting",
        "seed",
    ]
    assert project.branch() == "main"


def run_parent(project: Project) -> None:
    """A parent that runs a child task on a branch derived from its own."""
    service, parent, child = project.service, "TASK-3", "TASK-3/c1"

    service.start("parent", parent, agent="codex", init_artifact="Split the work.")
    assert project.branch() == "feature/task-3"
    split = service.next(parent)
    assert "./ww add-child TASK-3 --id <child-id>" in (split.action_text or "")
    service.add_child(parent, "c1", "Implement part one.")
    service.complete(parent, artifact="Split into one child.", summary_for_next="Done.")

    waiting = service.next(parent)
    assert waiting.action_kind == "child_workflow"
    assert "./ww start-child TASK-3 c1" in (waiting.action_text or "")

    started = service.start_child(parent, "c1")
    assert (started.task_id, started.item_name) == (child, "implement")
    assert project.branch() == "feature/task-3-c1"
    service.next(child)
    project.edit("part1.txt", "one\n")
    service.complete(
        child,
        variables=(("commit_message", "Implement part one"),),
        artifact="Implemented part one.",
        summary_for_next="Done.",
    )
    assert project.branch() == "feature/task-3"
    service.next(child)
    service.complete(
        child,
        variables=(("summary", "Part one done."),),
        summary_for_next="Done.",
    )

    finished = service.status(parent)
    assert finished.status == "completed"
    children = service.tasks.read_children(parent, "01-parent")
    assert [(entry.id, entry.status, entry.summary) for entry in children] == [
        ("c1", "completed", "Part one done.")
    ]
    assert project.commits("feature/task-3-c1") == [
        "TASK-3/c1: Implement part one",
        "seed",
    ]
    assert project.artifact(child, "implement").endswith("Implemented part one.\n")
    assert project.branch() == "main"


def test_standalone_workflow_with_items_group_and_command_hook(
    project: Project,
) -> None:
    run_feature(project)

    project.assert_nothing_written_by_ww()


def test_handoff_workflow_starts_its_successor(project: Project) -> None:
    run_triage(project)

    project.assert_nothing_written_by_ww()


def test_parent_runs_a_child_on_a_derived_branch(project: Project) -> None:
    run_parent(project)

    project.assert_nothing_written_by_ww()


def test_all_scenarios_share_one_repository(project: Project) -> None:
    run_feature(project)
    run_triage(project)
    run_parent(project)

    branches = {
        (record["task_id"], record["branch"], record["base"])
        for record in project.recorded("branches.jsonl")
    }
    assert branches == {
        ("TASK-1", "feature/task-1", "main"),
        ("TASK-2", "feature/task-2", "main"),
        ("TASK-3", "feature/task-3", "main"),
        ("TASK-3/c1", "feature/task-3-c1", "feature/task-3"),
    }
    commits = [
        (record["task_id"], record["workflow"], record["message"])
        for record in project.recorded("commits.jsonl")
    ]
    assert commits == [
        ("TASK-1", "feature", "TASK-1: Add a greeting"),
        ("TASK-2", "docs", "TASK-2: Document the greeting"),
        ("TASK-3/c1", "child", "TASK-3/c1: Implement part one"),
    ]
    assert all(
        project.tasks.task_exists(task) for task in ("TASK-1", "TASK-2", "TASK-3")
    )
    assert project.tasks.child_task_ids("TASK-3") == ("TASK-3/c1",)
    project.assert_nothing_written_by_ww()
