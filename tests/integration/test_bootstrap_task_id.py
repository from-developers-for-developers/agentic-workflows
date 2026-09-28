# SPDX-License-Identifier: GPL-3.0-or-later
from pathlib import Path

import pytest

from ww.errors import ConfigurationError
from ww.service import WorkflowService
from ww.storage import Storage
from ww.variables import BRANCH_NAMING_STRATEGY


def _service(tmp_path: Path) -> WorkflowService:
    (tmp_path / "ww-agentic-workflows.yaml").write_text(
        """workflows:
  - name: jira-task
    steps:
      - name: create-jira
        mcp: jira
        description: Create the Jira issue.
        provide:
          - name: task_id
      - name: develop
        description: Implement work for {{task_id}}.
""",
        encoding="utf-8",
    )
    return WorkflowService(Storage(tmp_path))


def test_start_without_id_bootstraps_and_binds_external_task_id(tmp_path: Path) -> None:
    service = _service(tmp_path)

    pending = service.start("jira-task", None, agent="codex")

    assert pending.task_id.startswith("REQUEST-")
    assert pending.stage == "Task identity bootstrap"
    assert not (tmp_path / ".ww" / "tasks").exists()

    active = service.next(pending.task_id)
    assert active.action_kind == "mcp"
    assert "`jira` mcp connection" in (active.action_text or "")
    assert active.required_values[0].name == "task_id"

    next_step = service.complete(
        pending.task_id,
        (("task_id", "PROJ-123"),),
        "Created Jira issue PROJ-123.",
        summary_for_next="Done.",
    )

    assert next_step.task_id == "PROJ-123"
    assert next_step.item_name == "develop"
    assert (tmp_path / ".ww" / "tasks" / "PROJ-123" / "runs" / "01-jira-task").exists()
    assert not (tmp_path / ".ww" / "tasks" / pending.task_id).exists()
    assert "PROJ-123" in service.next("PROJ-123").action_text
    service.complete("PROJ-123", artifact="Implemented work.", summary_for_next="Done.")
    artifact = (
        tmp_path
        / ".ww"
        / "tasks"
        / "PROJ-123"
        / "runs"
        / "01-jira-task"
        / "steps"
        / "02-create-jira.md"
    )
    assert artifact.read_text(encoding="utf-8") == (
        "# PROJ-123 — create-jira\n\n"
        "## Workflow context\n\n"
        "- Workflow: jira-task\n"
        "- Step: 1 of 1\n"
        "- Skill: auto\n\n"
        "## Result\n\n"
        "Created Jira issue PROJ-123.\n"
    )
    assert (
        tmp_path
        / ".ww"
        / "tasks"
        / "PROJ-123"
        / "runs"
        / "01-jira-task"
        / "steps"
        / "03-develop.md"
    ).exists()


def test_bootstrap_preserves_the_explicit_branch_naming_strategy(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    pending = service.start(
        "jira-task", None, agent="codex", branch_naming_strategy="experiment"
    )
    service.next(pending.task_id)

    service.complete(
        pending.task_id,
        (("task_id", "PROJ-STRATEGY"),),
        summary_for_next="Done.",
    )

    state = service.tasks.read_execution_state("PROJ-STRATEGY", "01-jira-task")
    assert state is not None
    assert dict(state.workflow_values)[BRANCH_NAMING_STRATEGY] == "experiment"


def test_explicit_task_id_is_authoritative_for_bootstrap_step(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.start("jira-task", "PROJ-123", agent="codex")
    service.next("PROJ-123")

    following = service.complete(
        "PROJ-123",
        artifact="Used existing Jira issue.",
        summary_for_next="Done.",
    )

    assert following.item_name == "develop"
    assert "PROJ-123" in (service.next("PROJ-123").action_text or "")


def test_task_id_provider_must_be_the_first_step(tmp_path: Path) -> None:
    service = _service(tmp_path)
    (tmp_path / "ww-agentic-workflows.yaml").write_text(
        """workflows:
  - name: jira-task
    steps:
      - name: prepare
        description: Prepare.
      - name: create-jira
        description: Create.
        provide:
          - name: task_id
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="only supported on the first"):
        service.start("jira-task", None, agent="codex")


def test_start_hooks_run_only_after_external_id_is_bound(tmp_path: Path) -> None:
    service = _service(tmp_path)
    (tmp_path / "ww-agentic-workflows.yaml").write_text(
        (tmp_path / "ww-agentic-workflows.yaml").read_text(encoding="utf-8")
        + """handlers:
  - name: record-start
    command:
      argv: [touch, bootstrap-hook-ran]
hooks:
  before_start_workflow:
    - name: record-start
""",
        encoding="utf-8",
    )

    pending = service.start("jira-task", None, agent="codex")

    assert not (tmp_path / "bootstrap-hook-ran").exists()
    service.next(pending.task_id)
    service.complete(
        pending.task_id,
        (("task_id", "PROJ-123"),),
        summary_for_next="Done.",
    )
    assert (tmp_path / "bootstrap-hook-ran").exists()


def test_bootstrap_binding_resumes_after_final_marker_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    pending = service.start("jira-task", None, agent="codex")
    service.next(pending.task_id)

    original = service.storage.write_bootstrap
    calls = 0

    def fail_final_marker(request_id: str, value: dict[str, object]) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected bootstrap marker failure")
        original(request_id, value)

    monkeypatch.setattr(service.storage, "write_bootstrap", fail_final_marker)
    with pytest.raises(OSError, match="injected bootstrap marker failure"):
        service.complete(
            pending.task_id,
            (("task_id", "PROJ-456"),),
            "Created Jira issue PROJ-456.",
            summary_for_next="Done.",
        )

    request = service.storage.read_bootstrap(pending.task_id)
    assert request is not None and request["status"] == "binding"
    assert (tmp_path / ".ww/tasks/PROJ-456/state.json").exists()

    monkeypatch.setattr(service.storage, "write_bootstrap", original)
    resumed = service.next(pending.task_id)
    assert resumed.task_id == "PROJ-456"
    request = service.storage.read_bootstrap(pending.task_id)
    assert request is not None and request["status"] == "completed"
    assert service.status("PROJ-456").task_id == "PROJ-456"


def test_bootstrap_binding_retries_when_start_dies_before_aggregate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    pending = service.start("jira-task", None, agent="codex")
    service.next(pending.task_id)
    original = service._start

    def fail_before_publish(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        raise OSError("injected pre-aggregate failure")

    monkeypatch.setattr(service, "_start", fail_before_publish)
    with pytest.raises(OSError, match="injected pre-aggregate failure"):
        service.complete(
            pending.task_id,
            (("task_id", "PROJ-789"),),
            summary_for_next="Done.",
        )

    request = service.storage.read_bootstrap(pending.task_id)
    assert request is not None and request["status"] == "binding"
    assert not (tmp_path / ".ww/tasks/PROJ-789/state.json").exists()

    monkeypatch.setattr(service, "_start", original)
    resumed = service.next(pending.task_id)
    assert resumed.task_id == "PROJ-789"
    assert service.storage.read_bootstrap(pending.task_id)["status"] == "completed"
    assert len(service.tasks.execution_runs("PROJ-789")) == 1
