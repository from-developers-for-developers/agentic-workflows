# SPDX-License-Identifier: GPL-3.0-or-later
"""Command output metadata is published with durable successful completion."""

from dataclasses import replace
from pathlib import Path

import pytest

from tests.workflow_helpers import configured_service, start_after_init
from ww.config import load_configuration
from ww.errors import ConfigurationError
from ww.execution_models import ExecutionState, PlanSnapshot
from ww.plan import compile_workflow_plan


def _config(command: str, extra: str = "") -> str:
    return f"""handlers:
  - publish: ~
    {command}
{extra}    saves:
      - metadata.github.pr_url: The pull request URL.
      - project_metadata.github.urls: Published URLs.
        append: true
workflows:
  - task: ~
    steps:
      - publish: ~
      - review: "Review {{{{ww.metadata.github.pr_url}}}}."
        artifact: false
"""


@pytest.mark.parametrize(
    "command",
    [
        "shell: \"printf '  https://example.test/pr/1\\n'; printf diagnostic >&2\"",
        'argv: [printf, "  https://example.test/pr/1\\n"]',
    ],
)
def test_command_saves_stdout_and_interpolates_next_step(
    tmp_path: Path, command: str
) -> None:
    service = configured_service(tmp_path, _config(command))
    start_after_init(service, "task", "TASK-1", agent="codex")
    page = service.next("TASK-1")
    assert page.item_name == "review"
    assert page.action_text == "Review https://example.test/pr/1."
    assert service.metadata_publisher.values("TASK-1") == {
        "ww.metadata.github.pr_url": "https://example.test/pr/1",
        "ww.project_metadata.github.urls": "https://example.test/pr/1",
    }
    state, snapshot = service.load("TASK-1")
    publish = next(item for item in snapshot.plan.items if item.name == "publish")
    assert publish.save_metadata[0].key == "github.pr_url"
    assert publish.save_metadata[0].source is None
    assert not state.pending_task_metadata
    assert state.pending_project_metadata is None


@pytest.mark.parametrize(
    "extra,command",
    [
        ("", 'shell: "printf url; exit 1"'),
        ("    assert:\n      - equals: expected\n", "argv: [printf, url]"),
    ],
)
def test_failed_commands_and_assertions_publish_nothing(
    tmp_path: Path,
    extra: str,
    command: str,
) -> None:
    service = configured_service(tmp_path, _config(command, extra))
    start_after_init(service, "task", "TASK-1", agent="codex")
    service.next("TASK-1")
    state, _ = service.load("TASK-1")
    assert state.status == "failed"
    assert service.metadata_publisher.values("TASK-1") == {}


def test_full_output_survives_snapshot_reload(tmp_path: Path) -> None:
    output = "x" * 2000
    service = configured_service(tmp_path, _config(f"argv: [printf, {output}]"))
    start_after_init(service, "task", "TASK-1", agent="codex")
    service.next("TASK-1")
    assert (
        service.metadata_publisher.values("TASK-1")["ww.metadata.github.pr_url"]
        == output
    )


@pytest.mark.parametrize("command", ['argv: [printf, url]', 'description: Work.'])
@pytest.mark.parametrize("source", ["stdout", "stderr"])
def test_invalid_command_metadata_mapping(
    tmp_path: Path,
    command: str,
    source: str,
) -> None:
    path = tmp_path / "ww.yaml"
    path.write_text(f"""handlers:
  - name: publish
    {command}
    saves:
      - metadata.url: URL.
        from: {source}
workflows:
  - task: ~
    steps:
      - publish: ~
""")
    with pytest.raises(ConfigurationError, match="unknown key.*from"):
        compile_workflow_plan(load_configuration(path), tmp_path, "task", "codex")


def test_publication_resumes_without_replaying_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = configured_service(
        tmp_path, _config('shell: "printf x >> calls; printf url"')
    )
    start_after_init(service, "task", "TASK-1", agent="codex")
    reconcile = service.metadata_publisher.reconcile

    def interrupted(
        state: ExecutionState, snapshot: PlanSnapshot
    ) -> tuple[ExecutionState, PlanSnapshot]:
        if state.pending_task_metadata:
            raise RuntimeError("publication interrupted")
        return reconcile(state, snapshot)

    monkeypatch.setattr(service.metadata_publisher, "reconcile", interrupted)
    with pytest.raises(RuntimeError, match="publication interrupted"):
        service.next("TASK-1")
    state, _ = service.load("TASK-1")
    assert state.pending_task_metadata
    assert service.metadata_publisher.values("TASK-1") == {}
    monkeypatch.setattr(service.metadata_publisher, "reconcile", reconcile)
    assert service.next("TASK-1").action_text == "Review url."
    assert (tmp_path / "calls").read_text() == "x"
    assert (
        service.metadata_publisher.values("TASK-1")["ww.project_metadata.github.urls"]
        == "url"
    )



def test_legacy_stdout_snapshot_roundtrips_and_runs(tmp_path: Path) -> None:
    service = configured_service(tmp_path, _config("argv: [printf, url]"))
    start_after_init(service, "task", "TASK-1", agent="codex")
    state, snapshot = service.load("TASK-1")
    plan = replace(
        snapshot.plan,
        items=tuple(
            replace(
                item,
                save_metadata=tuple(
                    replace(saved, source="stdout") for saved in item.save_metadata
                ),
            )
            for item in snapshot.plan.items
        ),
    )
    legacy = replace(snapshot, plan=plan)
    assert PlanSnapshot.from_dict(legacy.to_dict()).to_dict() == legacy.to_dict()
    service.commit(replace(state, plan_digest=legacy.plan_digest), legacy)
    assert service.next("TASK-1").action_text == "Review url."
