# SPDX-License-Identifier: GPL-3.0-or-later
"""Durable documents: free-format files workflows read and update across runs.

A document is declared once at the root of ``ww.yaml`` and lives in
the task directory, under ``.ww`` for the project scope, or in the user
configuration directory for the user scope, where every project of the user
shares it.  Agents edit the file in place; ww only resolves its path, checks
that a step which promised an update left the file behind, and journals who
updated it last.  The journal of a user document is the project's: it records
what this project's runs did to the shared file.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from ww.config_files import user_directory
from ww.errors import StateError
from ww.storage import Storage
from ww.validation import expect_optional_string, expect_string
from ww.workflow_config import TASK_ID_TOKEN, DocumentDefinition

JOURNAL_FILE = "documents.json"
DOCUMENTS_DIRECTORY = "documents"


@dataclass(frozen=True)
class DocumentUpdateRecord:
    """Who last updated a document, and the content they left."""

    updated_at: str
    run_id: str | None
    step: str
    sha256: str

    def to_dict(self) -> dict[str, str | None]:
        return {
            "updated_at": self.updated_at,
            "run_id": self.run_id,
            "step": self.step,
            "sha256": self.sha256,
        }

    @classmethod
    def from_dict(cls, data: object, label: str) -> DocumentUpdateRecord:
        if not isinstance(data, dict):
            raise ValueError(f"{label} must be a mapping")
        return cls(
            expect_string(data.get("updated_at"), f"{label}.updated_at"),
            expect_optional_string(data.get("run_id"), f"{label}.run_id"),
            expect_string(data.get("step"), f"{label}.step"),
            expect_string(data.get("sha256"), f"{label}.sha256"),
        )


class DocumentStore:
    """Paths and the update journal for declared documents."""

    def __init__(self, storage: Storage) -> None:
        self.storage = storage

    def path(
        self,
        document: DocumentDefinition,
        task_id: str | None,
        workspace: Path | None = None,
    ) -> Path:
        """The document's absolute file for the filesystem ww runs in now.

        A declared ``path`` is relative to the project root, or to the task's
        working directory for a task document when the run has one, so a file
        kept in the repository lands on the task's branch. A user document
        lives in the user configuration directory, which is created for it.
        """
        if document.scope == "user":
            return self._user_path(document)
        if document.path is None:
            directory = (
                self.storage.runtime_path / DOCUMENTS_DIRECTORY
                if document.scope == "project"
                else self._task_directory(task_id) / DOCUMENTS_DIRECTORY
            )
            return (directory / f"{document.name}.md").resolve()
        if document.scope == "task" and task_id is None:
            raise StateError("a task-scoped document needs a task ID")
        base = (
            workspace
            if document.scope == "task" and workspace is not None
            else self.storage.root
        )
        relative = document.path.replace(TASK_ID_TOKEN, task_id or "")
        return (base / relative).resolve()

    @staticmethod
    def _user_path(document: DocumentDefinition) -> Path:
        directory = user_directory().resolve()
        path = (directory / (document.path or f"{document.name}.md")).resolve()
        if not path.is_relative_to(directory):
            raise StateError(
                f"document {document.name!r} resolves outside the user "
                f"configuration directory: {path}"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def exists(
        self,
        document: DocumentDefinition,
        task_id: str | None,
        workspace: Path | None = None,
    ) -> bool:
        return self.path(document, task_id, workspace).is_file()

    def record_update(
        self,
        document: DocumentDefinition,
        task_id: str | None,
        *,
        run_id: str | None,
        step: str,
        updated_at: str,
        workspace: Path | None = None,
    ) -> DocumentUpdateRecord:
        """Journal that ``step`` updated the document, hashing its content."""
        path = self.path(document, task_id, workspace)
        if not path.is_file():
            raise StateError(f"document {document.name!r} does not exist at {path}")
        record = DocumentUpdateRecord(
            updated_at, run_id, step, hashlib.sha256(path.read_bytes()).hexdigest()
        )
        journal_path = self._journal_path(document, task_id)
        # A task journal is already serialized by the task lock; a project
        # journal is shared by every task, so its read-modify-write needs a
        # lock of its own, as project metadata has.
        with self.storage.locks.lock(journal_path, purpose="document journal"):
            journal = self._read_journal(journal_path)
            journal[document.name] = record.to_dict()
            self.storage.locks.atomic_write(
                journal_path, json.dumps(journal, indent=2, sort_keys=True) + "\n"
            )
        return record

    def last_update(
        self, document: DocumentDefinition, task_id: str | None
    ) -> DocumentUpdateRecord | None:
        journal_path = self._journal_path(document, task_id)
        entry = self._read_journal(journal_path).get(document.name)
        if entry is None:
            return None
        try:
            return DocumentUpdateRecord.from_dict(entry, document.name)
        except ValueError as error:
            raise StateError(
                f"invalid document journal {journal_path}: {error}"
            ) from error

    def listing(
        self,
        documents: tuple[DocumentDefinition, ...],
        task_id: str | None,
        workspace: Path | None = None,
    ) -> list[dict[str, object]]:
        """Describe every declared document: where it is and who updated it."""
        result: list[dict[str, object]] = []
        for document in documents:
            if document.scope == "task" and task_id is None:
                continue
            last = self.last_update(document, task_id)
            result.append(
                {
                    "name": document.name,
                    "description": document.description,
                    "scope": document.scope,
                    "path": str(self.path(document, task_id, workspace)),
                    "exists": self.exists(document, task_id, workspace),
                    "last_update": last.to_dict() if last else None,
                }
            )
        return result

    def remove_task(self, task_id: str) -> None:
        """Forget a task's documents: its journal and the files kept under ``.ww``.

        A document declared with an explicit ``path`` is the workflow's own
        file and stays in place.
        """
        directory = self._task_directory(task_id)
        (directory / JOURNAL_FILE).unlink(missing_ok=True)
        documents = directory / DOCUMENTS_DIRECTORY
        if documents.is_dir() and not documents.is_symlink():
            shutil.rmtree(documents)

    def _journal_path(self, document: DocumentDefinition, task_id: str | None) -> Path:
        if document.scope != "task":
            return self.storage.runtime_path / JOURNAL_FILE
        return self._task_directory(task_id) / JOURNAL_FILE

    def _task_directory(self, task_id: str | None) -> Path:
        if task_id is None:
            raise StateError("a task-scoped document needs a task ID")
        return self.storage.runtime_path / "tasks" / task_id

    @staticmethod
    def _read_journal(path: Path) -> dict[str, object]:
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise StateError(f"invalid document journal {path}: {error}") from error
        if not isinstance(data, dict):
            raise StateError(f"invalid document journal {path}: not a mapping")
        return data
