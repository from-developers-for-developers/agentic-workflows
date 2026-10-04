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
      - rounds: Review in rounds.
        max_rounds: 2
        loop:
          - fetch: ~
            argv: [python3, fetch.py]
            saves:
              - metadata.review.input: The fetched payload.
          - collect: Record one item per comment, using its source comment ID.
            items:
              identity: comment_id
              unique: [comment_id, reply_id]
              steps: []
          - analyze-together: Analyze all collected comments together.
          - confirm-analysis: Reuse the collected items.
            items:
              steps:
                - analyze: Check the shared analysis for this comment.
                  item_phase: analyze
          - fix-together: Implement the fixes for every analyzed comment.
          - resolve-each: Reuse the collected items.
            items:
              steps:
                - verify-resolution: Record this comment's own resolution.
                  item_phase: resolve
          - answer: Reuse the collected items.
            items:
              steps:
                - assess:
                    question: Does this comment get a reply?
                    outcomes:
                      positive:
                        steps:
                          - reply: ~
                            item_phase: report
                            on_failure: POLICY
                            argv:
                              - python3
                              - reply.py
                              - "{{ww.item.field.comment_id}}"
                              - "{{ww.item.actual_solution}}"
                              - "{{ww.item.field.reply_id}}"
                            saves:
                              - item.field.reply_id: The reply ID the script printed.
          - decide: Stop when no new comment arrived.
            break: No new comments.
"""


def _project(root: Path, policy: str = "fix") -> WorkflowService:
    (root / "ww.yaml").write_text(REVIEW.replace("POLICY", policy), encoding="utf-8")
    (root / "fetch.py").write_text(FETCH_SCRIPT, encoding="utf-8")
    (root / "reply.py").write_text(REPLY_SCRIPT, encoding="utf-8")
    service = WorkflowService(Storage(root))
    start_after_init(service, "review", TASK, agent="codex", workflow_runtime="single")
    return service


def _resumed(root: Path) -> WorkflowService:
    """A new process over the same task: status and instruction still render."""
    service = WorkflowService(Storage(root))
    assert main(["--root", str(root), "status", TASK]) in (0, 1)
    assert main(["--root", str(root), "instruction", TASK]) in (0, 1)
    return service


def _step(service: WorkflowService, artifact: str = "Done.") -> str | None:
    name = service.next(TASK).item_name
    service.complete(TASK, artifact=artifact, summary_for_next="Done.")
    return name


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


def _through_resolution(service: WorkflowService, root: Path) -> list[str]:
    """Round one up to the answer pass; the names of the steps the agent saw."""
    seen = [str(service.next(TASK).item_name)]
    assert (root / "fetch.log").read_text() == "fetch\n"  # ww ran the fetch
    service.add_item(TASK, _comment("c1", "Null check missing", "101"))
    service.add_item(TASK, _comment("c2", "Same null bug", "102", reference_to_id="c1"))
    service.add_item(TASK, _comment("c3", "Typo in the name", "103"))
    service.complete(TASK, artifact="Collected.", summary_for_next="Done.")
    seen.append(str(service.next(TASK).item_name))
    # One batch analysis finds the related pair; c2 points at c1.
    shared = "Both comments are the same missing null check in parse()."
    service.update_item(TASK, "c1", processed_item=shared, proposed_solution="Guard")
    service.update_item(TASK, "c2", processed_item=shared, proposed_solution="Guard")
    service.update_item(TASK, "c3", processed_item="Typo.", proposed_solution="Rename")
    before = _items(service)
    service.complete(TASK, artifact="Analyzed.", summary_for_next="Done.")
    seen += [str(_step(service)) for _ in range(4)]  # confirm-analysis + 3 checkpoints
    assert _items(service) == before  # the checkpoints investigated nothing again
    seen.append(str(_step(service, "One guard fixes both.")))  # the batch fix
    seen.append(str(_step(service)))  # the resolve pass's collection step
    for item_id in ("c1", "c2", "c3"):
        seen.append(str(service.next(TASK).item_name))
        service.update_item(
            TASK, item_id, actual_solution=f"Resolved {item_id}.", resolved=True
        )
        service.complete(TASK, artifact="Verified.", summary_for_next="Done.")
    return seen


def _answer_item(service: WorkflowService, outcome: str = "positive") -> Instruction:
    """Complete the item's assessment and choose its outcome."""
    assert service.next(TASK).item_name == "assess"
    service.complete(TASK, artifact="Answer it.", summary_for_next="Done.")
    return service.next(TASK, outcome=outcome)


def test_a_review_runs_from_fetch_to_replies_with_a_repair_a_resume_and_a_new_comment(
    tmp_path: Path,
) -> None:
    service = _project(tmp_path)

    seen = _through_resolution(service, tmp_path)

    assert seen == [
        "collect",
        "analyze-together",
        "confirm-analysis",
        "analyze",
        "analyze",
        "analyze",
        "fix-together",
        "resolve-each",
        "verify-resolution",
        "verify-resolution",
        "verify-resolution",
    ]
    # The fetch recorded its input as task metadata; the agent never ran it.
    metadata = json.loads(
        (tmp_path / ".ww/tasks" / TASK / "metadata.json").read_text(encoding="utf-8")
    )
    assert json.loads(metadata["metadata"]["review"]["input"])["comments"] == [
        "101",
        "102",
        "103",
    ]
    # Each comment keeps its own stable ID and its own resolution.
    assert {i.id: i.actual_solution for i in service.items(TASK)} == {
        "c1": "Resolved c1.",
        "c2": "Resolved c2.",
        "c3": "Resolved c3.",
    }

    assert service.next(TASK).item_name == "answer"
    service.complete(TASK, artifact="Reused.", summary_for_next="Done.")
    # c1 is answered; the remote then refuses c2.
    (tmp_path / "down").write_text("102")
    _answer_item(service)
    first = _items(service)["c1"]
    assert (first.reported, first.field("reply_id")) == (True, "r1")
    refused = _answer_item(service)
    assert refused.handler_repair is not None
    assert refused.item_name == "reply"

    # The session is interrupted; a new process sees the failure and a repair.
    service = _resumed(tmp_path)
    repair = service.instruction(TASK, caller_role="manager")
    assert repair.handler_repair is not None
    assert "remote refused comment 102" in (repair.action_text or "")
    assert _items(service)["c1"].field("reply_id") == "r1"
    assert not _items(service)["c2"].reported

    # A comment that arrives now joins the next round, not this frozen pass.
    service.add_item(TASK, _comment("c4", "Missing test", "104"))
    _, snapshot = service.load(TASK)
    assert not [i for i in snapshot.plan.items if i.item_id == "c4" and i.item_pass]

    (tmp_path / "down").unlink()
    service.complete(
        TASK,
        artifact="The remote accepts the reply again.",
        caller_role="worker",
        assignment=repair.assignment_token,
    )
    _answer_item(service)
    assert _ledger(tmp_path) == {
        "r1": {"comment": "101", "text": "Resolved c1."},
        "r2": {"comment": "102", "text": "Resolved c2."},
        "r3": {"comment": "103", "text": "Resolved c3."},
    }
    # The committed reply for 101 was never sent again; only 102 was retried.
    assert _lines(tmp_path / "calls.log") == ["101", "102", "102", "103"]
    assert _step_name(service) == "decide"
    service.complete(TASK, artifact="c4 arrived.", summary_for_next="Done.")

    # Round two: c4 is analyzed and fixed like the rest and gets its own reply,
    # while the committed replies of c1 to c3 are kept and not sent again.
    assert _step_name(service) == "collect"
    assert (tmp_path / "fetch.log").read_text() == "fetch\nfetch\n"
    service.complete(TASK, artifact="Nothing new.", summary_for_next="Done.")
    assert _step_name(service) == "analyze-together"
    service.update_item(
        TASK, "c4", processed_item="Needs a test.", proposed_solution="T"
    )
    service.complete(TASK, artifact="Analyzed.", summary_for_next="Done.")
    assert [_step(service) for _ in range(5)] == ["confirm-analysis"] + ["analyze"] * 4
    assert _step(service) == "fix-together"
    assert _step(service) == "resolve-each"
    for item_id in ("c1", "c2", "c3", "c4"):
        assert service.next(TASK).item_name == "verify-resolution"
        if item_id == "c4":
            service.update_item(TASK, item_id, actual_solution="Added.", resolved=True)
        service.complete(TASK, artifact="Verified.", summary_for_next="Done.")
    assert _step(service) == "answer"
    for _ in range(3):
        _answer_item(service, "negative")  # already answered in round one
    _answer_item(service)

    items = _items(service)
    assert [items[i].field("reply_id") for i in ("c1", "c2", "c3", "c4")] == [
        "r1",
        "r2",
        "r3",
        "r4",
    ]
    assert all(item.reported for item in items.values())
    assert _lines(tmp_path / "calls.log") == ["101", "102", "102", "103", "104"]
    assert set(_ledger(tmp_path)) == {"r1", "r2", "r3", "r4"}
    assert _step_name(service) == "decide"
    service.loop(TASK, artifact="No new comments.", summary_for_next="Done.")
    assert service.next(TASK).item_name == "update-workflow-summary"
    service.complete(TASK, (("summary", "Reviewed."),))
    assert service.load(TASK)[0].status == "completed"


def _step_name(service: WorkflowService) -> str | None:
    return service.next(TASK).item_name


def test_an_operator_stop_inside_an_item_stage_stays_inspectable_and_retries(
    tmp_path: Path,
) -> None:
    service = _project(tmp_path, "operator")
    _through_resolution(service, tmp_path)
    assert service.next(TASK).item_name == "answer"
    service.complete(TASK, artifact="Reused.", summary_for_next="Done.")
    _answer_item(service)
    (tmp_path / "down").write_text("102")

    stopped = _answer_item(service)

    assert stopped.control == "awaiting_operator"
    assert stopped.operator_reason == "handler_failed"
    assert stopped.handler_repair is None
    assert "remote refused comment 102" in (stopped.error or "")
    # Both views keep rendering for a new process; the stop exits nonzero.
    assert main(["--root", str(tmp_path), "status", TASK]) == 0
    assert main(["--root", str(tmp_path), "instruction", TASK]) == 1
    service = WorkflowService(Storage(tmp_path))
    assert service.status(TASK, caller_role="manager").operator_reason == (
        "handler_failed"
    )
    assert _items(service)["c1"].field("reply_id") == "r1"

    (tmp_path / "down").unlink()
    service.next(TASK, retry=True, caller_role="manager")  # the operator's pick
    assert _items(service)["c2"].field("reply_id") == "r2"
    assert _lines(tmp_path / "calls.log") == ["101", "102", "102"]
    _answer_item(service)
    assert set(_ledger(tmp_path)) == {"r1", "r2", "r3"}
    assert _step_name(service) == "decide"


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

PROFILE_INTERVIEW_WORDS = (
    "myrole",
    "ww-express",
    "ww.documents.me",
    "learned.me",
    "profile interview",
)

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


def _no_profile_interview_guidance(page: Instruction) -> None:
    rendered = MarkdownOutputAdapter().render_instruction(page).lower()
    for word in PROFILE_INTERVIEW_WORDS:
        assert word not in rendered, (page.item_name, word)
    assert "ww done" not in rendered


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
        _no_profile_interview_guidance(page)
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
        _no_profile_interview_guidance(page)
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
