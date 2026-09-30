# SPDX-License-Identifier: GPL-3.0-or-later
"""The "Handoff to manager" block ww writes when a worker's assignment ends."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests.workflow_helpers import assignment_token, start_after_init
from ww.errors import StateError
from ww.instructions import Instruction
from ww.output_adapters.json_adapter import JsonOutputAdapter
from ww.output_adapters.markdown import MarkdownOutputAdapter
from ww.service import SUMMARY_LIMIT, WorkflowService
from ww.storage import Storage

TASK = "TASK-1"

ROUND = """workflows:
  - task: ~
    steps:
      - review-and-fix: ~
        loop:
          - review: Review the implementation.
            break: There are no meaningful findings.
          - fix: Fix the review findings.
            continue: Findings remain for another round.
      - wrap-up: Wrap it up.
"""

CHECKED = """workflows:
  - name: task
    steps:
      - name: develop
        description: Develop it.
        hooks:
          before_complete:
            - argv: [sh, -c, "test ! -e broken"]
              on_failure: fix
      - check: Check it.
"""


def _auto(root: Path, workflows: str) -> WorkflowService:
    (root / "ww-agentic-workflows.yaml").write_text(workflows, encoding="utf-8")
    service = WorkflowService(Storage(root))
    start_after_init(
        service,
        "task",
        TASK,
        agent="codex",
        workflow_runtime="auto",
        caller_role="manager",
    )
    return service


def _worker_complete(
    service: WorkflowService, artifact: str, summary: str = "Done."
) -> Instruction:
    return service.complete(
        TASK,
        artifact=artifact,
        summary_for_next=summary,
        caller_role="worker",
        assignment=assignment_token(service, TASK),
    )


def _git(*arguments: str, cwd: Path) -> None:
    subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True)


def _repository(root: Path) -> None:
    _git("init", "-q", "-b", "main", ".", cwd=root)
    _git("config", "user.email", "t@e.st", cwd=root)
    _git("config", "user.name", "Test", cwd=root)
    (root / ".gitignore").write_text(".ww/\n", encoding="utf-8")
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    _git("add", "-A", cwd=root)
    _git("commit", "-qm", "seed", cwd=root)


def test_the_block_reports_every_step_of_the_assignment(tmp_path: Path) -> None:
    service = _auto(tmp_path, ROUND)
    service.next(TASK, caller_role="manager")
    token = assignment_token(service, TASK)
    assert token is not None

    fix = _worker_complete(service, "Findings.", "Two findings.")
    # The assignment goes on: no block yet.
    assert fix.item_name == "fix"
    assert fix.handoff_block is None

    ended = _worker_complete(service, "Fixed.", "Both findings fixed.")

    block = ended.handoff_block
    assert block is not None
    assert block.token == token
    assert [(step.name, step.outcome) for step in block.steps] == [
        ("review", "completed"),
        ("fix", "completed"),
    ]
    for step in block.steps:
        assert step.artifact is not None and Path(step.artifact).is_file()
        assert (step.checks, step.checks_waived, step.fix_rounds) == ((), (), 0)
    assert block.summary == "Both findings fixed."
    assert block.files is None
    assert block.error is None

    rendered = MarkdownOutputAdapter().render_instruction(ended)
    assert "### Handoff to manager" in rendered
    assert "Return the block below verbatim as your final message" in rendered
    assert f"Handoff to manager · assignment {token}" in rendered
    assert "- review: completed" in rendered
    assert "- fix: completed" in rendered
    assert "Worker summary: Both findings fixed." in rendered
    assert f"Manager: continue with `./ww next {TASK} --role manager`" in (
        rendered
    )
    assert "Files changed: not tracked" in rendered
    # The worker has nothing to run; the block names the manager's command.
    assert "### Manager command" not in rendered
    assert "a concise outcome" not in rendered

    payload = json.loads(JsonOutputAdapter().render_instruction(ended))
    assert payload["handoff_block"]["assignment"] == token
    assert [step["name"] for step in payload["handoff_block"]["steps"]] == [
        "review",
        "fix",
    ]


def test_the_block_names_a_loop_break(tmp_path: Path) -> None:
    service = _auto(tmp_path, ROUND)
    service.next(TASK, caller_role="manager")

    ended = service.loop(
        TASK,
        artifact="No findings.",
        summary_for_next="Clean.",
        caller_role="worker",
        assignment=assignment_token(service, TASK),
    )

    assert ended.handoff_block is not None
    assert [(s.name, s.outcome) for s in ended.handoff_block.steps] == [
        ("review", "loop break")
    ]


def test_the_block_names_a_loop_continue(tmp_path: Path) -> None:
    # A continue at the loop's limit stops the task for the operator, which
    # ends the worker's assignment; below the limit the worker goes on.
    service = _auto(
        tmp_path, ROUND.replace("loop:", "loop_max_times: 1\n        loop:", 1)
    )
    service.next(TASK, caller_role="manager")
    _worker_complete(service, "Findings.")

    ended = service.loop(
        TASK,
        artifact="Fixed some.",
        summary_for_next="More to do.",
        continue_loop=True,
        caller_role="worker",
        assignment=assignment_token(service, TASK),
    )

    # The round's records moved to the history; the block still has them.
    assert ended.loop_limit_reached
    assert ended.handoff_block is not None
    assert [(s.name, s.outcome) for s in ended.handoff_block.steps] == [
        ("review", "completed"),
        ("fix", "loop continue"),
    ]
    assert ended.handoff_block.summary == "More to do."


def test_the_block_reports_checks_fix_rounds_and_files(tmp_path: Path) -> None:
    _repository(tmp_path)
    service = _auto(tmp_path, CHECKED)
    service.next(TASK, caller_role="manager")
    (tmp_path / "broken").write_text("", encoding="utf-8")

    rejected = _worker_complete(service, "First try.")
    assert rejected.fix_required is not None
    assert rejected.handoff_block is None

    (tmp_path / "broken").unlink()
    (tmp_path / "feature.txt").write_text("new\n", encoding="utf-8")
    ended = _worker_complete(service, "Second try.")

    block = ended.handoff_block
    assert block is not None
    (develop,) = block.steps
    assert (develop.name, develop.outcome, develop.fix_rounds) == (
        "develop",
        "completed",
        1,
    )
    assert develop.checks and all(status == "passed" for _, status in develop.checks)
    assert block.files == ("feature.txt",)
    rendered = MarkdownOutputAdapter().render_instruction(ended)
    assert "  fix rounds: 1" in rendered
    assert "Files changed:\n- feature.txt" in rendered


def test_a_failed_assignment_carries_the_error(tmp_path: Path) -> None:
    service = _auto(tmp_path, ROUND)
    service.next(TASK, caller_role="manager")
    token = assignment_token(service, TASK)
    _worker_complete(service, "Findings.")

    failed = service.fail(
        TASK, "The build tool is missing.", caller_role="worker", assignment=token
    )

    block = failed.handoff_block
    assert block is not None
    assert [(step.name, step.outcome) for step in block.steps] == [
        ("review", "completed"),
        ("fix", "failed"),
    ]
    message = "agent item 'fix' failed: The build tool is missing."
    assert block.steps[1].error == message
    # The failed step carries the run's error; it is not repeated.
    assert block.error is None
    rendered = MarkdownOutputAdapter().render_instruction(failed)
    assert f"  error: {message}" in rendered
    assert 'Return the "Handoff to manager" block below to the manager' in rendered


def test_a_handler_failure_after_the_last_completion_is_the_blocks_error(
    tmp_path: Path,
) -> None:
    (tmp_path / "ww-agentic-workflows.yaml").write_text(
        """workflows:
  - name: task
    steps:
      - name: work
        description: Work.
        hooks:
          after_complete:
            - argv: [sh, -c, "echo nope >&2; exit 3"]
      - more: More.
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))
    start_after_init(service, "task", TASK, agent="codex", workflow_runtime="auto")
    service.next(TASK, caller_role="manager")

    failed = _worker_complete(service, "Worked.")

    block = failed.handoff_block
    assert failed.status == "failed"
    assert block is not None
    assert [(step.name, step.outcome) for step in block.steps] == [
        ("work", "completed")
    ]
    assert block.error


def test_the_single_runtime_has_no_block(tmp_path: Path) -> None:
    (tmp_path / "ww-agentic-workflows.yaml").write_text(ROUND, encoding="utf-8")
    service = WorkflowService(Storage(tmp_path))
    start_after_init(service, "task", TASK, agent="codex")
    service.next(TASK)

    page = service.complete(
        TASK, artifact="Findings.", summary_for_next="Done.", caller_role="worker"
    )

    assert page.handoff_block is None


def test_the_summary_is_capped(tmp_path: Path) -> None:
    service = _auto(tmp_path, ROUND)
    service.next(TASK, caller_role="manager")

    with pytest.raises(StateError, match=f"keep it to {SUMMARY_LIMIT}"):
        _worker_complete(service, "Findings.", "x" * (SUMMARY_LIMIT + 1))

    assert _worker_complete(service, "Findings.", "x" * SUMMARY_LIMIT).item_name == (
        "fix"
    )


def test_the_bootstrap_page_announces_the_block_by_token(tmp_path: Path) -> None:
    service = _auto(tmp_path, ROUND)

    page = service.next(TASK, caller_role="manager")

    rendered = MarkdownOutputAdapter().render_instruction(page)
    assert (
        f'"Handoff to manager" block for assignment `{page.assignment_token}`'
        in rendered
    )


def test_a_retried_handlers_values_carry_the_open_token(tmp_path: Path) -> None:
    (tmp_path / "ww-agentic-workflows.yaml").write_text(
        """handlers:
  - name: gate
    provide:
      - name: value
        description: Must be ok.
    shell: test "$VALUE" = ok
    env:
      VALUE: "{{value}}"
hooks:
  before_complete:
    - steps: [work]
      name: gate
workflows:
  - name: task
    steps:
      - name: work
        prompt: true
""",
        encoding="utf-8",
    )
    service = WorkflowService(Storage(tmp_path))
    start_after_init(service, "task", TASK, agent="codex", workflow_runtime="auto")
    service.next(TASK, caller_role="manager")
    token = assignment_token(service, TASK)
    failed = service.complete(
        TASK,
        (("value", "bad"),),
        "# work\n",
        summary_for_next="Done.",
        caller_role="worker",
        assignment=token,
    )
    assert failed.status == "failed"

    asked = service.next(TASK, retry=True, caller_role="manager")

    assert asked.item_status == "awaiting_input"
    assert assignment_token(service, TASK) == token
    assert f"--role worker --assignment {token}" in (asked.continuation_command or "")
    done = service.complete(
        TASK, (("value", "ok"),), caller_role="worker", assignment=token
    )
    assert done.status != "failed"
