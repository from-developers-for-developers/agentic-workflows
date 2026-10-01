# SPDX-License-Identifier: GPL-3.0-or-later
from pathlib import Path

import pytest

from tests.workflow_helpers import assignment_token, start_after_init
from ww.errors import StateError
from ww.service import WorkflowService
from ww.storage import Storage
from ww.storage_adapters import ProjectMetadata, TaskMetadata
from ww.storage_adapters.memory import MemoryTaskStorageAdapter


def test_worker_assignment_state_round_trips_through_memory_adapter(
    tmp_path: Path,
) -> None:
    (tmp_path / "ww.yaml").write_text(
        """handlers:
  - name: document
    description: Document it.
workflows:
  - name: task
    steps:
      - name: work
        hooks:
          after_complete:
            - name: document
""",
        encoding="utf-8",
    )
    persistence = MemoryTaskStorageAdapter()
    service = WorkflowService(Storage(tmp_path), persistence)
    service.start("task", "TASK-1", agent="codex", caller_role="manager")
    service.next(
        "TASK-1",
        model="worker-model",
        reasoning="high",
        caller_role="manager",
    )

    hook = service.complete(
        "TASK-1",
        artifact="implemented",
        caller_role="worker", assignment=assignment_token(service, "TASK-1"),
        summary_for_next="Done.",
    )
    assert hook.item_name == "document"
    state = persistence.read_execution_state("TASK-1", "01-task")
    assert state is not None
    assert state.assignment_item_id == "task:work:step:step:1"
    assert state.assignment_model == "worker-model"

    reloaded = WorkflowService(Storage(tmp_path), persistence)
    visible = reloaded.status(
        "TASK-1", caller_role="worker", assignment=assignment_token(reloaded, "TASK-1")
    )
    assert visible.item_name == "document"
    assert visible.control == "continue_worker"


def test_reset_remains_available_without_loading_workflow_configuration(
    tmp_path: Path,
) -> None:
    persistence = MemoryTaskStorageAdapter()
    service = WorkflowService(Storage(tmp_path), persistence)

    result = service.reset("TASK-1")

    assert result.task_id == "TASK-1"
    assert not result.removed


def test_generated_task_id_start_works_without_shared_storage(tmp_path: Path) -> None:
    (tmp_path / "ww.yaml").write_text(
        """workflows:
  - name: task
    steps: []
""",
        encoding="utf-8",
    )
    persistence = MemoryTaskStorageAdapter()
    service = WorkflowService(Storage(tmp_path), persistence)

    instruction = start_after_init(service, "task", None, agent="codex")

    assert instruction.task_id.startswith("TASK-")
    assert persistence.read_task_aggregate(instruction.task_id)[0]


def test_configured_task_format_drives_generated_ids(tmp_path: Path) -> None:
    (tmp_path / "ww.json").write_text(
        '{"task_format": "WORK-{{digit}}"}', encoding="utf-8"
    )
    (tmp_path / "ww.yaml").write_text(
        """workflows:
  - name: task
    steps:
      - name: work
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))

    first = start_after_init(service, "task", None, agent="codex")
    second = start_after_init(service, "task", None, agent="codex")

    assert first.task_id == "WORK-1"
    assert second.task_id == "WORK-2"


def test_configured_uuid_task_format_drives_generated_ids(tmp_path: Path) -> None:
    (tmp_path / "ww.json").write_text(
        '{"task_format": "TASK-{{uuid}}"}', encoding="utf-8"
    )
    (tmp_path / "ww.yaml").write_text(
        """workflows:
  - name: task
    steps: []
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))

    instruction = start_after_init(service, "task", None, agent="codex")

    assert instruction.task_id.startswith("TASK-")
    assert len(instruction.task_id) == 41


def test_generated_task_id_skips_existing_task_directories(tmp_path: Path) -> None:
    (tmp_path / "ww.json").write_text(
        '{"task_format": "TASK-{{digit}}"}', encoding="utf-8"
    )
    (tmp_path / "ww.yaml").write_text(
        """workflows:
  - name: task
    steps:
      - name: work
""",
        encoding="utf-8",
    )
    (tmp_path / ".ww" / "tasks" / "TASK-1").mkdir(parents=True)
    (tmp_path / ".ww" / "tasks" / "TASK-2").mkdir()
    service = WorkflowService(Storage(tmp_path))

    instruction = start_after_init(service, "task", None, agent="codex")

    assert instruction.task_id == "TASK-3"


def test_generated_task_id_skips_existing_configured_worktree(tmp_path: Path) -> None:
    (tmp_path / "ww.yaml").write_text(
        """workflows:
  - name: task
    steps: []
""",
        encoding="utf-8",
    )
    (tmp_path / "ww.json").write_text(
        """{
  "task_format": "TASK-{{digit}}",
  "extensions": {
    "ww/git": {
      "worktrees": true,
      "worktree_dir": "worktrees",
      "worktree_name_format": "{{ww.task.id}}"
    }
  }
}
""",
        encoding="utf-8",
    )
    (tmp_path / "worktrees" / "TASK-1").mkdir(parents=True)
    service = WorkflowService(Storage(tmp_path))

    instruction = start_after_init(service, "task", None, agent="codex")

    assert instruction.task_id == "TASK-2"


def test_task_metadata_is_read_through_the_adapter(tmp_path: Path) -> None:
    (tmp_path / "ww.yaml").write_text(
        """workflows:
  - name: task
    steps:
      - name: inspect
        mcp: github
        description: Inspect {{ww.metadata.github.repository}}.
""",
        encoding="utf-8",
    )
    persistence = MemoryTaskStorageAdapter()
    persistence.write_task_metadata(
        TaskMetadata("TASK-1", (("github.repository", "openai/ww"),))
    )
    service = WorkflowService(Storage(tmp_path), persistence)

    start_after_init(service, "task", "TASK-1", agent="codex")
    instruction = service.next("TASK-1")

    assert "Inspect openai/ww." in (instruction.action_text or "")


def test_agent_saves_task_metadata_for_later_steps(tmp_path: Path) -> None:
    (tmp_path / "ww.yaml").write_text(
        """workflows:
  - name: task
    steps:
      - name: create
        description: Create the Jira issue.
        artifact: false
        saves:
          - metadata.foo.bar.baz.jira_id: Preserve the created issue ID.
      - name: inspect
        description: Inspect {{ww.metadata.foo.bar.baz.jira_id}}.
        artifact: false
""",
        encoding="utf-8",
    )
    persistence = MemoryTaskStorageAdapter()
    service = WorkflowService(Storage(tmp_path), persistence)

    start_after_init(service, "task", "TASK-1", agent="codex")
    create = service.next("TASK-1")

    assert create.required_metadata[0].key == "foo.bar.baz.jira_id"
    assert '--metadata foo.bar.baz.jira_id="<foo.bar.baz.jira_id>"' in (
        create.continuation_command or ""
    )

    with pytest.raises(
        StateError, match="missing required task metadata.*foo.bar.baz.jira_id"
    ):
        service.complete("TASK-1", summary_for_next="Done.")
    with pytest.raises(StateError, match="unexpected task metadata.*other"):
        service.complete(
            "TASK-1",
            metadata_values=(("other", "value"),),
            summary_for_next="Done.",
        )

    service.complete(
        "TASK-1",
        metadata_values=(("foo.bar.baz.jira_id", "PROJ-123"),),
        summary_for_next="Done.",
    )
    inspect = service.next("TASK-1")

    assert inspect.action_text == "Inspect PROJ-123."
    metadata = persistence.read_task_metadata("TASK-1")
    assert metadata is not None
    assert metadata.to_dict() == {"foo": {"bar": {"baz": {"jira_id": "PROJ-123"}}}}


def test_metadata_is_not_published_when_completion_commit_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "ww.yaml").write_text(
        """workflows:
  - name: task
    steps:
      - name: capture
        artifact: false
        saves:
          - metadata.result.task:
          - project_metadata.result.project:
""",
        encoding="utf-8",
    )
    persistence = MemoryTaskStorageAdapter()
    service = WorkflowService(Storage(tmp_path), persistence)
    start_after_init(service, "task", "TASK-1", agent="codex")
    service.next("TASK-1")
    original = service.commit

    def fail_commit(*args: object, **kwargs: object) -> None:
        raise StateError("injected completion commit failure")

    monkeypatch.setattr(service, "commit", fail_commit)
    with pytest.raises(StateError, match="injected completion commit failure"):
        service.complete(
            "TASK-1",
            metadata_values=(
                ("result.task", "task-result"),
                ("project_metadata.result.project", "project-result"),
            ),
            summary_for_next="Done.",
        )

    assert service.metadata("TASK-1") == {}
    assert service.project_metadata() == {}

    monkeypatch.setattr(service, "commit", original)
    service.complete(
        "TASK-1",
        metadata_values=(
            ("result.task", "task-result"),
            ("project_metadata.result.project", "project-result"),
        ),
        summary_for_next="Done.",
    )
    assert persistence.read_task_metadata("TASK-1") == TaskMetadata(
        "TASK-1", (("result.task", "task-result"),)
    )
    assert service.project_metadata() == {"result": {"project": "project-result"}}


def test_project_metadata_retry_detects_an_intervening_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "ww.yaml").write_text(
        """workflows:
  - name: task
    steps:
      - name: capture
        artifact: false
        saves:
          - project_metadata.result.value:
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))
    start_after_init(service, "task", "TASK-1", agent="codex")
    service.next("TASK-1")
    original = service.project_metadata_store.write_project_metadata
    calls = 0

    def fail_once(metadata: ProjectMetadata) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise StateError("injected project publication failure")
        original(metadata)

    monkeypatch.setattr(
        service.project_metadata_store, "write_project_metadata", fail_once
    )
    with pytest.raises(StateError, match="injected project publication failure"):
        service.complete(
            "TASK-1",
            metadata_values=(("project_metadata.result.value", "produced"),),
            summary_for_next="Done.",
        )

    original(ProjectMetadata((("result.value", "intervening"),)))
    with pytest.raises(StateError, match="project metadata publication conflict"):
        service.next("TASK-1")
    assert service.project_metadata() == {"result": {"value": "intervening"}}


def test_project_metadata_publication_is_idempotent_after_clear_commit_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "ww.yaml").write_text(
        """workflows:
  - name: task
    steps:
      - name: capture
        artifact: false
        saves:
          - project_metadata.result.value:
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))
    start_after_init(service, "task", "TASK-1", agent="codex")
    service.next("TASK-1")
    original = service.commit
    commits = 0

    def fail_after_publication(*args: object, **kwargs: object) -> None:
        nonlocal commits
        commits += 1
        if commits == 2:
            raise StateError("injected publication-clear commit failure")
        original(*args, **kwargs)

    monkeypatch.setattr(service, "commit", fail_after_publication)
    with pytest.raises(StateError, match="injected publication-clear commit failure"):
        service.complete(
            "TASK-1",
            metadata_values=(("project_metadata.result.value", "published"),),
            summary_for_next="Done.",
        )
    assert service.project_metadata() == {"result": {"value": "published"}}

    monkeypatch.setattr(service, "commit", original)
    service.next("TASK-1")
    assert service.project_metadata() == {"result": {"value": "published"}}


def test_saved_task_metadata_is_available_to_automatic_commands(tmp_path: Path) -> None:
    (tmp_path / "ww.yaml").write_text(
        """workflows:
  - name: task
    steps:
      - name: capture
        description: Capture the value.
        artifact: false
        saves:
          - metadata.service.token:
      - name: write
        artifact: false
        shell: printf '%s' "$TOKEN" > metadata.txt
        env:
          TOKEN: "{{ww.metadata.service.token}}"
""",
        encoding="utf-8",
    )
    persistence = MemoryTaskStorageAdapter()
    service = WorkflowService(Storage(tmp_path), persistence)

    start_after_init(service, "task", "TASK-1", agent="codex")
    service.next("TASK-1")
    service.complete(
        "TASK-1",
        metadata_values=(("service.token", "secret-ref"),),
        summary_for_next="Done.",
    )

    assert (tmp_path / "metadata.txt").read_text() == "secret-ref"


def test_saved_project_metadata_is_available_to_automatic_commands(
    tmp_path: Path,
) -> None:
    (tmp_path / "ww.yaml").write_text(
        """workflows:
  - name: task
    steps:
      - name: capture
        description: Capture the value.
        artifact: false
        saves:
          - project_metadata.service.url:
      - name: write
        artifact: false
        shell: printf '%s' "$URL" > project-metadata.txt
        env:
          URL: "{{ww.project_metadata.service.url}}"
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))

    start_after_init(service, "task", "TASK-1", agent="codex")
    service.next("TASK-1")
    service.complete(
        "TASK-1",
        metadata_values=(
            ("project_metadata.service.url", "https://staging.example.com"),
        ),
        summary_for_next="Done.",
    )

    assert (tmp_path / "project-metadata.txt").read_text() == (
        "https://staging.example.com"
    )
