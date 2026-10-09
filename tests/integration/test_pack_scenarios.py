# SPDX-License-Identifier: GPL-3.0-or-later
"""The workflow usability pack end to end: a fake PR review and a fake setup.

Everything here goes through the public service and CLI boundaries in a
temporary project; the "remote" is a project-owned ledger script and no network
is used.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests.workflow_helpers import start_after_init
from ww.cli import main
from ww.errors import StateError
from ww.instructions import Instruction
from ww.items import WorkItem
from ww.output_adapters.markdown import MarkdownOutputAdapter
from ww.service import WorkflowService
from ww.storage import Storage

TASK = "TASK-9"

FETCH_SCRIPT = """import json
open("fetch.log", "a").write("fetch\\n")
comments = ["101", "102", "103"]
print(json.dumps({"pr": "https://example.invalid/pr/9", "comments": comments}))
"""

# The project's own fake remote. Given an existing reply ID it updates that
# reply instead of creating another; every call is logged so a replay shows.
REPLY_SCRIPT = """import json, os, sys
comment, text, reply_id = sys.argv[1:4]
open("calls.log", "a").write(comment + "\\n")
if os.path.exists("down") and open("down").read().strip() == comment:
    print("remote refused comment " + comment)
    sys.exit(7)
ledger = json.load(open("ledger.json")) if os.path.exists("ledger.json") else {}
if not reply_id:
    reply_id = "r%d" % (len(ledger) + 1)
ledger[reply_id] = {"comment": comment, "text": text}
json.dump(ledger, open("ledger.json", "w"))
print(reply_id)
"""

REVIEW = """workflows:
  - name: review
    steps:
      - review-pass: Review the comments received so far.

        steps:
          - fetch: ~
            argv: [python3, fetch.py]
            saves:
              - metadata.review.input: The fetched payload.
          - feedback: Record one item per comment, using its source comment ID.
            items:
              identity: comment_id
              unique: [comment_id, reply_id]
              steps:
                - develop: Fix related comments together; resolve each one.
                - lint: ~
                  argv: [python3, lint.py]
                  on_failure: POLICY
                - send-replies: >-
                    Reply in the thread of {{ww.item.field.comment_id}} for
                    every comment; mark each reported after its reply succeeds.
                  saves:
                    - item.field.reply_id: The reply ID the remote returned.
          - decide: Summarize the review outcome.

"""

LINT_SCRIPT = """import os, sys
open("lint.log", "a").write("lint\\n")
if os.path.exists("lint-broken"):
    print("lint failed")
    sys.exit(3)
"""


def _project(root: Path, policy: str = "fix") -> WorkflowService:
    (root / "ww.yaml").write_text(REVIEW.replace("POLICY", policy), encoding="utf-8")
    (root / "fetch.py").write_text(FETCH_SCRIPT, encoding="utf-8")
    (root / "reply.py").write_text(REPLY_SCRIPT, encoding="utf-8")
    (root / "lint.py").write_text(LINT_SCRIPT, encoding="utf-8")
    service = WorkflowService(Storage(root))
    start_after_init(service, "review", TASK, agent="codex", workflow_runtime="single")
    return service


def _resumed(root: Path) -> WorkflowService:
    """A new process over the same task: status and instruction still render."""
    service = WorkflowService(Storage(root))
    assert main(["--root", str(root), "status", TASK]) in (0, 1)
    assert main(["--root", str(root), "instruction", TASK]) in (0, 1)
    return service


def _items(service: WorkflowService) -> dict[str, WorkItem]:
    return {item.id: item for item in service.items(TASK)}


def _comment(item_id: str, text: str, comment_id: str, **extra: str) -> WorkItem:
    return WorkItem(
        item_id,
        text,
        reference_to_id=extra.get("reference_to_id"),
        fields=(("comment_id", comment_id),),
    )


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").split() if path.exists() else []


def _ledger(root: Path) -> dict[str, dict[str, str]]:
    return json.loads((root / "ledger.json").read_text(encoding="utf-8"))


def _reply(service: WorkflowService, root: Path, item_id: str) -> bool:
    """What the agent does per thread: post, then record the ID and the report."""
    item = service.item(TASK, item_id)
    posted = subprocess.run(
        [
            "python3",
            "reply.py",
            str(item.field("comment_id")),
            item.actual_solution,
            item.field("reply_id") or "",
        ],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if posted.returncode != 0:
        return False
    service.update_item(TASK, item_id, fields={"reply_id": posted.stdout.strip()})
    service.report_item(TASK, item_id)
    return True


def _through_development(service: WorkflowService, root: Path) -> Instruction:
    """Fetch, register three comments, fix them together, and pass the lint."""
    assert service.next(TASK).item_name == "feedback"
    assert (root / "fetch.log").read_text() == "fetch\n"  # ww ran the fetch
    service.add_item(TASK, _comment("c1", "Null check missing", "101"))
    service.add_item(TASK, _comment("c2", "Same null bug", "102", reference_to_id="c1"))
    service.add_item(TASK, _comment("c3", "Typo in the name", "103"))
    service.complete(TASK, artifact="Collected.", summary_for_next="Done.")
    assert service.next(TASK).item_name == "develop"
    # One guard fixes c1 and c2; each comment still records its own outcome.
    for item_id, outcome in (
        ("c1", "Guarded parse()."),
        ("c2", "Handled together with c1."),
        ("c3", "Renamed."),
    ):
        service.resolve_item(TASK, item_id, actual_solution=outcome)
    return service.complete(
        TASK, artifact="One guard fixes both.", summary_for_next="Done."
    )


def test_a_review_runs_from_fetch_to_replies_with_a_resume_and_a_new_comment(
    tmp_path: Path,
) -> None:
    service = _project(tmp_path)
    _through_development(service, tmp_path)
    _, snapshot = service.load(TASK)
    steps = [item.name for item in snapshot.plan.items]

    replies = service.next(TASK)
    assert replies.item_name == "send-replies"
    assert _lines(tmp_path / "lint.log") == ["lint"]  # ww ran the lint once
    assert "--get field.comment_id" in (replies.action_text or "")
    # The fetch recorded its input as task metadata; the agent never ran it.
    metadata = json.loads(
        (tmp_path / ".ww/tasks" / TASK / "metadata.json").read_text(encoding="utf-8")
    )
    assert json.loads(metadata["metadata"]["review"]["input"])["comments"] == [
        "101",
        "102",
        "103",
    ]

    # c1 is answered; the remote then refuses c2, which stays unreported.
    assert _reply(service, tmp_path, "c1")
    (tmp_path / "down").write_text("102")
    assert not _reply(service, tmp_path, "c2")
    assert not _items(service)["c2"].reported

    # The session is interrupted; a new process sees the same step and records.
    service = _resumed(tmp_path)
    assert service.instruction(TASK).item_name == "send-replies"
    assert _items(service)["c1"].field("reply_id") == "r1"
    with pytest.raises(StateError, match="c2: field.reply_id"):
        service.complete(TASK, artifact="Replied.", summary_for_next="Done.")

    # A comment that arrives now joins the same collection and its gate.
    service.add_item(TASK, _comment("c4", "Missing test", "104"))
    service.resolve_item(TASK, "c4", actual_solution="Added a test.")
    (tmp_path / "down").unlink()
    for item_id in ("c2", "c3", "c4"):
        assert _reply(service, tmp_path, item_id)
    service.complete(TASK, artifact="Replied.", summary_for_next="Done.")

    assert _ledger(tmp_path) == {
        "r1": {"comment": "101", "text": "Guarded parse()."},
        "r2": {"comment": "102", "text": "Handled together with c1."},
        "r3": {"comment": "103", "text": "Renamed."},
        "r4": {"comment": "104", "text": "Added a test."},
    }
    # The committed reply for 101 was never sent again; only 102 was retried.
    assert _lines(tmp_path / "calls.log") == ["101", "102", "102", "103", "104"]
    assert all(item.reported for item in _items(service).values())
    # Four comments did not add a step: the plan is the one compiled at start.
    assert [item.name for item in service.load(TASK)[1].plan.items] == steps
    assert service.next(TASK).item_name == "decide"
    service.complete(TASK, artifact="Reviewed.", summary_for_next="Done.")
    assert service.next(TASK).item_name == "update-workflow-summary"
    service.complete(TASK, (("summary", "Reviewed."),))
    assert service.load(TASK)[0].status == "completed"


def test_an_operator_stop_inside_an_items_context_stays_inspectable_and_retries(
    tmp_path: Path,
) -> None:
    service = _project(tmp_path, "operator")
    (tmp_path / "lint-broken").touch()

    stopped = _through_development(service, tmp_path)
    assert _lines(tmp_path / "lint.log") == ["lint"]

    assert stopped.control == "awaiting_operator"
    assert stopped.operator_reason == "handler_failed"
    assert stopped.handler_repair is None
    assert "lint failed" in (stopped.error or "")
    # Both views keep rendering for a new process; the stop exits nonzero.
    assert main(["--root", str(tmp_path), "status", TASK]) == 0
    assert main(["--root", str(tmp_path), "instruction", TASK]) == 1
    service = WorkflowService(Storage(tmp_path))
    assert service.status(TASK, caller_role="manager").operator_reason == (
        "handler_failed"
    )
    assert all(item.resolved for item in _items(service).values())

    (tmp_path / "lint-broken").unlink()
    page = service.next(TASK, retry=True, caller_role="manager")  # the operator's pick
    assert _lines(tmp_path / "lint.log") == ["lint", "lint"]
    assert page.item_name == "send-replies"
    assert all(item.resolved for item in _items(service).values())


WORKTREE_WORKFLOW = """hooks:
  before_start:
    - steps: [fetch]
      handlers:
        - ext/ww/git/handlers:start-task-branch: ~
        - ext/ww/git/handlers:create-worktree: ~
workflows:
  - name: pr
    steps:
      - fetch: ~
        shell: printf 'https://example.invalid/pr/%s\\n' "$1"
        args: ["{{ww.task.id}}"]
        saves:
          - metadata.pull_request.url: The pull request URL.
      - show: ~
        shell: printf '%s|%s|%s|%s\\n' "$1" "$2" "$3" "$PWD"
        args:
          - "{{ww.task.id}}"
          - "{{ww.metadata.pull_request.url}}"
          - "{{ww.task.workspace_dir}}"
        saves:
          - metadata.pull_request.seen: What the step saw.
"""


def test_runtime_values_and_metadata_follow_the_task_not_the_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path.resolve()
    (root / ".gitignore").write_text(".ww/\ntrees/\n", encoding="utf-8")
    (root / "ww.json").write_text(
        json.dumps(
            {
                "extensions": {
                    "ww/git": {
                        "base_branches": {"default": "main"},
                        "worktrees": True,
                        "worktree_dir": "./trees",
                        "worktree_name_format": "{{ww.task.id}}",
                        "branch_name_formats": {"default": "feature/{{ww.task.id}}"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (root / "ww.yaml").write_text(WORKTREE_WORKFLOW, encoding="utf-8")
    for command in (
        ("git", "init", "-q", "-b", "main", "."),
        ("git", "config", "user.email", "t@e.st"),
        ("git", "config", "user.name", "Test"),
        ("git", "config", "commit.gpgsign", "false"),
        ("git", "add", "-A"),
        ("git", "commit", "-qm", "seed"),
    ):
        subprocess.run(command, cwd=root, check=True, capture_output=True)
    service = WorkflowService(Storage(root))
    for task in ("TASK-A", "TASK-B"):
        start_after_init(service, "pr", task, agent="codex", workflow_runtime="single")
        while service.load(task)[0].status != "completed":
            if service.next(task).item_name == "update-workflow-summary":
                service.complete(task, (("summary", "Done."),))
            else:
                service.complete(task, artifact="Done.", summary_for_next="Done.")

    for task in ("TASK-A", "TASK-B"):
        worktree = root / "trees" / task
        saved = json.loads(
            (root / ".ww/tasks" / task / "metadata.json").read_text(encoding="utf-8")
        )["metadata"]["pull_request"]
        # The PR URL was saved for this task and read back by the later step.
        assert saved["url"] == f"https://example.invalid/pr/{task}"
        assert saved["seen"] == f"{task}|{saved['url']}|{worktree}|{worktree}"
        # The worktree holds the checkout only, never the task's store.
        assert not (worktree / ".ww/tasks").exists()

    # From inside a worktree the CLI reads the primary checkout's store.
    monkeypatch.chdir(root / "trees/TASK-A")
    assert main(["metadata", "TASK-A"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["pull_request"]["url"].endswith("/TASK-A")
    assert main(["metadata", "TASK-B"]) == 0
    assert json.loads(capsys.readouterr().out)["pull_request"]["url"].endswith(
        "/TASK-B"
    )


# --- setup: learn the project, discuss the process, propose, validate, apply ---

PROPOSAL = """workflows:
  - name: hotfix
    description: Fix fast.
    steps:
      - patch: Patch it.
      - verify: Run the checks.
"""
UPDATED_HOTFIX = """workflows:
  - name: hotfix
    description: Fix fast, with a regression test first.
    steps:
      - test: Write the failing test.
      - patch: Patch it.
      - verify: Run the checks.
"""


def _discuss(
    service: WorkflowService, page: Instruction, said: str, prefer: str = ""
) -> Instruction:
    """A brief recorded conversation, ended by recording it, not by a phrase."""
    task = page.task_id
    service.interact(
        task, transcript=f"Agent: A question.\nOperator: {said}", caller_role="manager"
    )
    labels = [choice.label for choice in page.choices]
    options = {"choice": prefer if prefer in labels else labels[0]} if labels else {}
    return service.interact(task, end=True, caller_role="manager", **options)


def _no_magic_closing_phrase(page: Instruction) -> None:
    rendered = MarkdownOutputAdapter().render_instruction(page).lower()
    assert "ww done" not in rendered, page.item_name


@pytest.mark.usefixtures("shipped_builtins")
def test_a_fresh_project_learns_discusses_proposes_validates_and_updates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "ww.yaml").write_text(
        "workflows:\n  - name: task\n    steps:\n      - work: Work.\n",
        encoding="utf-8",
    )
    (root / "ww.json").write_text("{}", encoding="utf-8")
    service = WorkflowService(Storage(root))
    arguments = ["--root", str(root)]

    # Learn the project: no step interviews the operator about themselves.
    start_after_init(service, "ww-learn-project", "L-1", agent="codex")
    learned = []
    while (page := service.next("L-1", caller_role="manager")).item_name != (
        "update-workflow-summary"
    ):
        learned.append(page.item_name)
        assert page.interactive == (page.item_name == "review")
        _no_magic_closing_phrase(page)
        if page.item_name == "review":
            (root / ".ww").mkdir(exist_ok=True)
            (root / ".ww/project.md").write_text(
                "# Project\n\nA small service; tests run with `pytest`.\n",
                encoding="utf-8",
            )
            _discuss(service, page, "That matches the project.")
        service.complete(
            "L-1", artifact="Done.", summary_for_next="Done.", caller_role="manager"
        )
    assert learned == ["scan", "history", "review", "finish"]

    # A brief process discussion, then a minimal proposal.
    start_after_init(service, "ww-suggest", "S-1", agent="codex")
    names = []
    while (page := service.next("S-1", caller_role="manager")).item_name != (
        "update-workflow-summary"
    ):
        names.append(page.item_name)
        _no_magic_closing_phrase(page)
        if page.item_name == "assess":
            service.complete(
                "S-1",
                artifact="As chosen.",
                summary_for_next="Done.",
                caller_role="manager",
            )
            service.next("S-1", outcome="positive", caller_role="manager")
            continue
        if page.interactive:
            assert "clear contextual completion" in (
                MarkdownOutputAdapter().render_instruction(page)
            )
            _discuss(
                service,
                page,
                "Reviews are slow; I want fixes tested first.",
                prefer={"design": "for me", "propose": "apply"}.get(page.item_name, ""),
            )
        if page.item_name == "propose":
            (root / ".ww/tasks/S-1/setup-proposal.yaml").write_text(
                PROPOSAL, encoding="utf-8"
            )
        service.complete(
            "S-1", artifact="Done.", summary_for_next="Done.", caller_role="manager"
        )
    assert names == [
        "gather",
        "process",
        "design",
        "assess",
        "propose",
        "assess",
        "apply",
    ]
    proposal = root / ".ww/tasks/S-1/setup-proposal.yaml"

    # Composed validation shows the plan the proposal would leave; nothing is
    # written, and then the operator's "apply" places it.
    assert (
        main(
            [
                *arguments,
                "setup",
                "apply",
                str(proposal),
                "--for",
                "me",
                "--dry-run",
                "--inspect",
                "hotfix",
                "--agent",
                "codex",
            ]
        )
        == 0
    )
    assert "Run the checks." in capsys.readouterr().out
    assert not (root / "ww-setup.local.yaml").exists()
    assert (
        main([*arguments, "setup", "apply", str(proposal), "--for", "me", "--yes"]) == 0
    )
    assert "hotfix" in (root / "ww-setup.local.yaml").read_text(encoding="utf-8")
    assert main([*arguments, "lint"]) == 0
    capsys.readouterr()

    # A later change goes through `setup update`, validated the same way.
    update = tmp_path / "hotfix.yaml"
    update.write_text(UPDATED_HOTFIX, encoding="utf-8")
    assert (
        main(
            [
                *arguments,
                "setup",
                "update",
                "hotfix",
                str(update),
                "--dry-run",
                "--inspect",
                "hotfix",
                "--agent",
                "codex",
            ]
        )
        == 0
    )
    assert "Write the failing test." in capsys.readouterr().out
    assert "Write the failing" not in (root / "ww-setup.local.yaml").read_text(
        encoding="utf-8"
    )
    assert main([*arguments, "setup", "update", "hotfix", str(update), "--yes"]) == 0
    local = (root / "ww-setup.local.yaml").read_text(encoding="utf-8")
    assert "Write the failing test." in local
    assert "Fix fast, with a regression test first." in local
    assert local.count("name: hotfix") == 1
    assert main([*arguments, "lint"]) == 0
