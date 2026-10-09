# SPDX-License-Identifier: GPL-3.0-or-later
"""``{{ww.git.*}}``: the ww/git template namespace in a running task."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests.workflow_helpers import start_after_init
from ww.errors import ConfigurationError
from ww.output_adapters.markdown import MarkdownOutputAdapter
from ww.service import WorkflowService
from ww.storage import Storage

BRANCHING = """hooks:
  before_start_workflow:
    - workflows: [feature]
      handlers:
        - ext/ww/git/handlers:start-task-branch: ~
workflows:
  - name: feature
    steps:
      - review: Review {{ww.git.branch}} against {{ww.git.base_branch}}.
      - land:
        argv: [echo, "{{ww.git.branch}}"]
  - name: unbranched
    steps:
      - review: Review {{ww.git.branch}}.
"""

GIT_SETTINGS = {
    "separate_branch": True,
    "base_branches": {"default": "main"},
    "branch_name_formats": {"default": "feature/{{ww.task.id}}"},
}


def _root(
    tmp_path: Path,
    workflows: str = BRANCHING,
    extensions: dict[str, object] | None = None,
) -> Path:
    for args in (
        ("init", "-q", "-b", "main", "."),
        ("config", "user.email", "t@e.st"),
        ("config", "user.name", "Test"),
        ("config", "commit.gpgsign", "false"),
    ):
        subprocess.run(("git", *args), cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / ".gitignore").write_text(".ww/\n", encoding="utf-8")
    subprocess.run(("git", "add", "-A"), cwd=tmp_path, check=True)
    subprocess.run(
        ("git", "commit", "-qm", "seed"), cwd=tmp_path, check=True, capture_output=True
    )
    (tmp_path / "ww.yaml").write_text(workflows, encoding="utf-8")
    (tmp_path / "ww.json").write_text(
        json.dumps(
            {
                "enabled": True,
                "extensions": (
                    {"ww/git": GIT_SETTINGS} if extensions is None else extensions
                ),
            }
        ),
        encoding="utf-8",
    )
    return tmp_path.resolve()


def test_a_step_description_reads_the_task_branch(tmp_path: Path) -> None:
    service = WorkflowService(Storage(_root(tmp_path)))

    start_after_init(service, "feature", "T1", agent="codex")
    instruction = service.next("T1")

    assert instruction.action_text is not None
    assert "Review feature/t1 against main." in instruction.action_text


def test_an_automatic_handler_reads_the_task_branch(tmp_path: Path) -> None:
    service = WorkflowService(Storage(_root(tmp_path)))
    start_after_init(service, "feature", "T1", agent="codex")
    service.next("T1")

    service.complete("T1", artifact="Reviewed.", summary_for_next="Done.")

    outputs = [
        service.tasks.read_command_output(entry["command_output"]).strip()
        for entry in service.artifacts("T1")
        if entry.get("stream") == "stdout"
    ]
    assert outputs == ["feature/t1"]


def test_a_branch_not_recorded_yet_stops_for_the_operator(tmp_path: Path) -> None:
    service = WorkflowService(Storage(_root(tmp_path)))
    start_after_init(service, "unbranched", "T1", agent="codex")

    stopped = service.next("T1")

    assert stopped.control == "awaiting_operator"
    assert stopped.operator_reason == "value_unavailable"
    assert stopped.error is not None
    assert "{{ww.git.branch}} (provided by ww/git)" in stopped.error
    assert {command.action for command in stopped.recovery_commands} == {
        "retry",
        "force",
    }
    rendered = MarkdownOutputAdapter().render_instruction(stopped)
    assert "a value the step reads is not available yet" in rendered
    assert "The step has not started" in rendered
    # Every page of the task still builds: nothing is bricked.
    assert service.instruction("T1").operator_reason == "value_unavailable"
    assert service.instruction_status("T1", None).operator_reason == "value_unavailable"


def test_retry_checks_the_values_again(tmp_path: Path) -> None:
    service = WorkflowService(Storage(_root(tmp_path)))
    start_after_init(service, "unbranched", "T1", agent="codex")
    service.next("T1")

    still = service.next("T1", retry=True)
    assert still.operator_reason == "value_unavailable"

    # The operator created the branch, and ww/git recorded it.
    service.extensions.store("ww/git").append_line(
        "branches.jsonl",
        json.dumps({"task_id": "T1", "branch": "feature/t1", "base": "main"}),
    )
    assert service.next("T1", retry=True).operator_reason is None
    resumed = service.next("T1")

    assert resumed.action_text is not None
    assert "Review feature/t1." in resumed.action_text


def test_force_skips_the_step_that_cannot_start(tmp_path: Path) -> None:
    service = WorkflowService(Storage(_root(tmp_path)))
    start_after_init(service, "unbranched", "T1", agent="codex")
    service.next("T1")

    skipped = service.next("T1", force=True, force_reason="No branch for this one.")

    assert skipped.operator_reason is None
    assert skipped.item_name == "update-workflow-summary"


def test_an_unknown_namespace_is_still_an_error(tmp_path: Path) -> None:
    service = WorkflowService(
        Storage(
            _root(
                tmp_path,
                "workflows:\n  - name: feature\n    steps:\n"
                "      - review: Review {{ww.hg.branch}}.\n",
            )
        )
    )

    with pytest.raises(ConfigurationError, match=r"ww\.hg\.branch"):
        service.start("feature", "T1", agent="codex")


def test_an_unknown_git_variable_is_an_error(tmp_path: Path) -> None:
    service = WorkflowService(
        Storage(
            _root(
                tmp_path,
                "workflows:\n  - name: feature\n    steps:\n"
                "      - review: Review {{ww.git.tag}}.\n",
            )
        )
    )

    with pytest.raises(ConfigurationError, match=r"ww\.git\.tag"):
        service.start("feature", "T1", agent="codex")


def test_the_namespace_needs_ww_git_in_the_settings(tmp_path: Path) -> None:
    service = WorkflowService(
        Storage(
            _root(
                tmp_path,
                "workflows:\n  - name: feature\n    steps:\n"
                "      - review: Review {{ww.git.branch}}.\n",
                extensions={},
            )
        )
    )

    with pytest.raises(ConfigurationError, match=r"ww\.git\.branch"):
        service.start("feature", "T1", agent="codex")


def test_a_provided_value_may_not_start_with_ww(tmp_path: Path) -> None:
    service = WorkflowService(
        Storage(
            _root(
                tmp_path,
                "workflows:\n  - name: feature\n    steps:\n"
                "      - review: Review.\n"
                "        variables:\n"
                "          - ww.git.branch: The branch.\n",
            )
        )
    )

    with pytest.raises(ConfigurationError, match="reserved"):
        service.start("feature", "T1", agent="codex")


def test_a_workflow_with_a_lane_branches_as_that_lane(tmp_path: Path) -> None:
    root = _root(
        tmp_path,
        BRANCHING
        + """  - name: hotfix
    steps:
      - fix: Fix it.
  - name: chores
    hooks_from: hotfix
    steps:
      - review: Review {{ww.git.branch}} as {{ww.git.branch_strategy}}.
""",
        {
            "ww/git": {
                **GIT_SETTINGS,
                "base_branches": {"default": "main", "hotfix": "stable"},
                "branch_name_formats": {
                    "default": "feature/{{ww.task.id}}",
                    "hotfix": "hotfix/{{ww.task.id}}",
                },
            }
        },
    )
    (root / "ww.yaml").write_text(
        (root / "ww.yaml")
        .read_text(encoding="utf-8")
        .replace("workflows: [feature]", "workflows: [hotfix]", 1),
        encoding="utf-8",
    )
    subprocess.run(("git", "branch", "stable"), cwd=root, check=True)
    service = WorkflowService(Storage(root))

    start_after_init(service, "chores", "C1", agent="codex")
    instruction = service.next("C1")

    assert instruction.action_text is not None
    assert "Review hotfix/c1 as hotfix." in instruction.action_text
    lines = (root / ".ww/ext/ww/git/branches.jsonl").read_text(encoding="utf-8")
    record = json.loads(lines.splitlines()[-1])
    assert (record["task_id"], record["workflow"], record["base"]) == (
        "C1",
        "chores",
        "stable",
    )
