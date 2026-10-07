# SPDX-License-Identifier: GPL-3.0-or-later
"""A handler's inputs are consumed when it runs: the next consumer asks afresh."""

from __future__ import annotations

from pathlib import Path

from tests.workflow_helpers import assignment_token, configured_service

# ``commit`` and ``tag`` both want ``commit_message``; ``work`` feeds both in
# one completion window, the loop's commit hook lies outside every window.
WORKFLOWS = """handlers:
  - name: commit
    variables:
      - name: commit_message
    shell: printf "%s\\n" "$MESSAGE" >> commits.txt
    env:
      MESSAGE: "{{commit_message}}"
  - name: tag
    variables:
      - name: commit_message
    shell: printf "tag %s\\n" "$MESSAGE" >> commits.txt
    env:
      MESSAGE: "{{commit_message}}"
workflows:
  - name: task
    steps:
      - name: work
        hooks:
          after_complete:
            - name: commit
            - name: tag
      - name: review-and-fix
        hooks:
          after_complete:
            - name: commit
        loop:
          - name: review
            break: Clean.
"""


def test_a_later_consumer_asks_for_its_own_value(tmp_path: Path) -> None:
    service = configured_service(tmp_path, WORKFLOWS)
    service.start(
        "task",
        "TASK-1",
        agent="codex",
        workflow_runtime="auto",
        init_artifact="Do it.",
        caller_role="manager",
    )
    work = service.next("TASK-1", caller_role="manager")
    assert work.item_name == "work"

    # The worker's completion feeds both hooks of its window with one value.
    service.complete(
        "TASK-1",
        artifact="Worked.",
        variables=(("commit_message", "Add work"),),
        summary_for_next="Work done.",
        caller_role="worker",
        assignment=assignment_token(service, "TASK-1"),
    )
    assert (tmp_path / "commits.txt").read_text(encoding="utf-8") == (
        "Add work\ntag Add work\n"
    )
    state, _ = service.load("TASK-1")
    assert "commit_message" not in dict(state.workflow_values)

    review = service.next("TASK-1", caller_role="manager")
    assert review.item_name == "review"
    service.loop(
        "TASK-1",
        artifact="Clean.",
        summary_for_next="Nothing to fix.",
        caller_role="worker",
        assignment=assignment_token(service, "TASK-1"),
    )

    # The loop's commit hook asks for its own message instead of reusing the
    # one ``work`` supplied.
    request = service.next("TASK-1", caller_role="manager")
    assert request.status == "awaiting_input"
    assert [value.name for value in request.required_values] == ["commit_message"]
    assert (request.next_role, request.manager_input) == ("manager", True)
    done = service.complete(
        "TASK-1",
        variables=(("commit_message", "Fix review"),),
        caller_role="manager",
        summary_for_next="Done.",
    )
    assert done.status != "awaiting_input"
    assert (tmp_path / "commits.txt").read_text(encoding="utf-8") == (
        "Add work\ntag Add work\nFix review\n"
    )
