# SPDX-License-Identifier: GPL-3.0-or-later
"""Durable documents: declared once, edited in place, journaled by ww."""

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

WORKFLOWS = """documents:
  - test_cases: The test cases derived from the issue.
  - conventions: Conventions shared by every task.
    scope: project
workflows:
  - name: task
    steps:
      - name: derive
        description: "Derive test cases into {{documents.test_cases}}."
        update_document:
          - test_cases: One checklist item per case; keep items that still hold.
      - name: report
        description: >-
          Report from {{documents.test_cases}}; conventions in
          {{documents.conventions}}.
        artifact: false
"""


def _service(tmp_path: Path) -> WorkflowService:
    (tmp_path / "ww-agentic-workflows.yaml").write_text(WORKFLOWS, encoding="utf-8")
    return WorkflowService(Storage(tmp_path))


def test_documents_resolve_to_task_and_project_files(tmp_path: Path) -> None:
    service = _service(tmp_path)
    start_after_init(service, "task", "TASK-1", agent="codex")
    task_file = (tmp_path / ".ww/tasks/TASK-1/documents/test_cases.md").resolve()
    project_file = (tmp_path / ".ww/documents/conventions.md").resolve()

    derive = service.next("TASK-1")
    assert derive.action_text == f"Derive test cases into {task_file}."
    (document,) = derive.documents
    assert (document.name, document.path, document.exists) == (
        "test_cases",
        str(task_file),
        False,
    )
    rendered = MarkdownOutputAdapter().render_instruction(derive)
    assert "### Documents to update" in rendered
    assert f"- `test_cases` at `{task_file}` (does not exist yet; create it)" in (
        rendered
    )
    assert "One checklist item per case; keep items that still hold." in rendered

    listing = service.documents_listing("TASK-1")
    assert [(entry["name"], entry["scope"], entry["exists"]) for entry in listing] == [
        ("test_cases", "task", False),
        ("conventions", "project", False),
    ]
    assert listing[1]["path"] == str(project_file)
    # Without a task, only project documents can be located.
    assert [entry["name"] for entry in service.documents_listing(None)] == [
        "conventions"
    ]


def test_a_step_that_promised_a_document_must_leave_it_behind(tmp_path: Path) -> None:
    service = _service(tmp_path)
    start_after_init(service, "task", "TASK-1", agent="codex")
    service.next("TASK-1")
    task_file = tmp_path / ".ww/tasks/TASK-1/documents/test_cases.md"

    with pytest.raises(StateError, match="promised to update document 'test_cases'"):
        service.complete("TASK-1", artifact="derived", summary_for_next="Derived.")
    assert not (tmp_path / ".ww/tasks/TASK-1/documents.json").exists()

    task_file.parent.mkdir(parents=True)
    task_file.write_text("- [ ] login works\n", encoding="utf-8")
    report = service.complete("TASK-1", artifact="derived", summary_for_next="Derived.")
    assert report.item_name == "report"
    assert report.documents == ()

    journal = json.loads(
        (tmp_path / ".ww/tasks/TASK-1/documents.json").read_text(encoding="utf-8")
    )
    entry = journal["test_cases"]
    assert (entry["step"], entry["run_id"]) == ("derive", "01-task")
    assert len(entry["sha256"]) == 64
    (test_cases, _conventions) = service.documents_listing("TASK-1")
    assert test_cases["exists"] is True
    assert test_cases["last_update"]["step"] == "derive"

    # A later run finds the document and its provenance in place.
    service.next("TASK-1")
    service.complete("TASK-1", summary_for_next="Reported.")
    service.next("TASK-1")
    service.complete("TASK-1", variables=(("summary", "Done."),))
    service.start("task", "TASK-1", agent="codex", init_artifact="Issue updated.")
    derive = service.next("TASK-1")
    (document,) = derive.documents
    assert document.exists is True
    assert f"- `test_cases` at `{task_file.resolve()}` (exists)" in (
        MarkdownOutputAdapter().render_instruction(derive)
    )


def test_the_documents_command_prints_the_listing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    service = _service(tmp_path)
    start_after_init(service, "task", "TASK-1", agent="codex")

    assert main(["--root", str(tmp_path), "documents", "TASK-1"]) == 0
    listing = json.loads(capsys.readouterr().out)
    assert [entry["name"] for entry in listing] == ["test_cases", "conventions"]
    assert listing[0]["path"].endswith("/.ww/tasks/TASK-1/documents/test_cases.md")
    assert listing[0]["last_update"] is None


def test_a_declared_path_resolves_in_the_project_or_the_task_workspace(
    tmp_path: Path,
) -> None:
    (tmp_path / "ww-agentic-workflows.yaml").write_text(
        """documents:
  - notes: Notes kept with the repository.
    path: documentation/issues/{task_id}/notes.md
  - glossary: Shared terms.
    scope: project
    path: docs/glossary.md
workflows:
  - name: task
    steps:
      - name: note
        description: "Write {{documents.notes}}; terms in {{documents.glossary}}."
        update_document:
          - notes: Keep the notes current.
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))
    start_after_init(service, "task", "TASK-7", agent="codex")

    note = service.next("TASK-7")
    notes_file = (tmp_path / "documentation/issues/TASK-7/notes.md").resolve()
    glossary_file = (tmp_path / "docs/glossary.md").resolve()
    assert note.action_text == f"Write {notes_file}; terms in {glossary_file}."
    assert note.documents[0].path == str(notes_file)

    # Inside a task workspace the task document follows the checkout, the
    # project document does not.
    store = service.documents
    workspace = tmp_path / "trees/TASK-7"
    notes, glossary = service._load_configuration().documents
    assert (
        store.path(notes, "TASK-7", workspace)
        == (workspace / "documentation/issues/TASK-7/notes.md").resolve()
    )
    assert store.path(glossary, "TASK-7", workspace) == glossary_file

    notes_file.parent.mkdir(parents=True)
    notes_file.write_text("# Notes\n", encoding="utf-8")
    service.complete("TASK-7", artifact="noted", summary_for_next="Noted.")
    journal = json.loads(
        (tmp_path / ".ww/tasks/TASK-7/documents.json").read_text(encoding="utf-8")
    )
    assert journal["notes"]["step"] == "note"


def test_reset_forgets_the_task_documents_and_interactions(tmp_path: Path) -> None:
    """A reset task leaves no journal behind for a task started under its ID."""
    service = _service(tmp_path)
    start_after_init(service, "task", "TASK-1", agent="codex")
    service.next("TASK-1")
    task_directory = tmp_path / ".ww/tasks/TASK-1"
    task_file = task_directory / "documents/test_cases.md"
    task_file.parent.mkdir(parents=True)
    task_file.write_text("- [ ] login works\n", encoding="utf-8")
    service.complete("TASK-1", artifact="derived", summary_for_next="Derived.")
    service.interactions.append(
        "TASK-1", run_id="01-task", step="report", speaker="operator", text="Hi", at="t"
    )
    assert (task_directory / "documents.json").is_file()
    assert (task_directory / "interactions.md").is_file()

    assert service.reset("TASK-1").removed is True

    assert not task_directory.exists()
    start_after_init(service, "task", "TASK-1", agent="codex")
    (test_cases, _conventions) = service.documents_listing("TASK-1")
    assert (test_cases["exists"], test_cases["last_update"]) == (False, None)
    assert service.interactions.entries("TASK-1") == ()
