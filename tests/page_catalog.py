# SPDX-License-Identifier: GPL-3.0-or-later
"""A fixed set of real instruction pages, rendered from fixture workflows.

The golden page tests (``tests/integration/test_golden_pages.py``), the gate
tests (``tests/integration/test_page_gates.py``) and the size measurement
(``scripts/measure_pages.py``) share this set, so a wording change shows up
both as a diff and as a number. Each scenario drives a real
``WorkflowService`` in its own directory and returns the page an agent would
read at that point; ``render_page`` makes what differs between runs fixed (the
assignment token) or replaces it (the directory, commit hashes, command-output
directories).
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from ww.instructions import Instruction
from ww.items import WorkItem
from ww.output_adapters.markdown import MarkdownOutputAdapter
from ww.service import WorkflowService
from ww.storage import Storage

GOLDEN_DIRECTORY = Path(__file__).resolve().parent / "golden" / "pages"
"""Where the golden page tests keep the expected pages."""

TOKEN = "0a1b2c3d"
"""Every assignment token in the catalog, so pages are reproducible."""

TASK = "TASK-1"

STEPS = """workflows:
  - name: task
    steps:
      - name: develop
        description: Implement the change described in the requirements.
        hooks:
          before_complete:
            - argv: [touch, developed.txt]
      - name: review
        description: Review the change.
        subagents: false
"""

VALUES = """workflows:
  - name: task
    steps:
      - name: create-issue
        description: Create the tracker issue for the task.
        variables:
          - priority: The issue's priority, one of low, medium, high.
        saves:
          - metadata.jira.issue_id: The created issue key.
"""

INTERACTIVE = """workflows:
  - name: task
    steps:
      - name: confirm
        description: Confirm the result with the operator.
        interactive: true
        choices:
          - accept: The result is accepted.
          - reject: The result needs more work.
"""

LOOP = """workflows:
  - name: task
    steps:
      - review-and-fix: ~
        loop:
          - review: Review the implementation.
            break: There are no meaningful findings.
          - fix: Fix the review findings.
            continue: Findings remain for another round.
"""

ITEMS = """workflows:
  - name: task
    steps:
      - name: collect
        description: Collect the review comments.
        items:
          steps:
            - name: process
              description: Process it.
              item_phase: analyze
            - name: resolve
              description: Resolve it.
              item_phase: resolve
"""

FAILING = """handlers:
  - name: reject
    argv: ["false"]
workflows:
  - name: task
    steps:
      - name: work
        description: Do the work.
        hooks:
          before_complete:
            - name: reject
"""

RULES = """workflows:
  - name: task
    steps:
      - name: develop
        description: Develop it.
        rules:
          - Keep the public CLI unchanged.
        hooks:
          before_complete:
            - argv: [sh, -c, "test ! -e broken"]
              on_failure: fix
      - check: Check it.
"""


def _service(root: Path, config: str) -> WorkflowService:
    (root / "ww-agentic-workflows.yaml").write_text(config, encoding="utf-8")
    return WorkflowService(Storage(root))


def _start(
    root: Path, config: str, runtime: str = "single"
) -> tuple[WorkflowService, Instruction]:
    service = _service(root, config)
    started = service.start(
        "task",
        TASK,
        agent="codex",
        workflow_runtime=runtime,
        init_artifact="Add a --verbose flag to the CLI.",
        caller_role="manager",
    )
    return service, started



def step_single(root: Path) -> Instruction:
    """A step in the single runtime, after the previous step's handover."""
    service, _ = _start(root, STEPS)
    service.next(TASK)
    service.complete(TASK, artifact="Implemented.", summary_for_next="Added the flag.")
    return service.next(TASK)


def manager_dispatch(root: Path) -> Instruction:
    """The manager's page in ``auto`` before the next assignment is dispatched."""
    _, started = _start(root, STEPS, "auto")
    return started


def worker_bootstrap(root: Path) -> Instruction:
    """The manager hands the selected worker its bootstrap command."""
    service, _ = _start(root, STEPS, "auto")
    return service.next(TASK, caller_role="manager")


def step_auto_worker(root: Path) -> Instruction:
    """The worker's page in ``auto``, read with its assignment token."""
    service, _ = _start(root, STEPS, "auto")
    service.next(TASK, caller_role="manager")
    return service.instruction(TASK, caller_role="worker", assignment=TOKEN)


def worker_handoff(root: Path) -> Instruction:
    """The worker's last page of an assignment: ww's handoff block."""
    service, _ = _start(root, STEPS, "auto")
    service.next(TASK, caller_role="manager")
    return service.complete(
        TASK,
        artifact="Implemented.",
        summary_for_next="Added the flag.",
        caller_role="worker",
        assignment=TOKEN,
    )


def loop_body(root: Path) -> Instruction:
    """A loop body step that may break the loop."""
    service, _ = _start(root, LOOP)
    return service.next(TASK)


def loop_continue(root: Path) -> Instruction:
    """A loop body step that may start the loop over."""
    service, _ = _start(root, LOOP)
    service.next(TASK)
    service.complete(TASK, artifact="Two findings.", summary_for_next="Two findings.")
    return service.next(TASK)


def item_stage(root: Path) -> Instruction:
    """A per-item stage after the items were collected."""
    service, _ = _start(root, ITEMS)
    service.next(TASK)
    service.add_item(TASK, WorkItem("comment-1", "Rename the flag."))
    service.complete(TASK, artifact="Collected.", summary_for_next="One comment.")
    return service.next(TASK)


def values_and_metadata(root: Path) -> Instruction:
    """A step that supplies a value and saves task metadata on completion."""
    service, _ = _start(root, VALUES)
    return service.next(TASK)


def interactive(root: Path) -> Instruction:
    """An interactive step: a conversation with the operator in this session."""
    service, _ = _start(root, INTERACTIVE)
    return service.next(TASK)


def step_rules(root: Path) -> Instruction:
    """A step with a judged rule and a check that runs at completion."""
    _git_project(root)
    service, _ = _start(root, RULES)
    return service.next(TASK)


def operator_stop(root: Path) -> Instruction:
    """A failed automatic handler: the task waits for the operator."""
    service, _ = _start(root, FAILING)
    service.next(TASK)
    return service.complete(TASK, artifact="Done.", summary_for_next="Done.")


def completion(root: Path) -> Instruction:
    """The last page of a completed workflow."""
    service, _ = _start(root, STEPS)
    page = service.next(TASK)
    while page.status != "completed":
        if page.item_status == "pending":
            page = service.next(TASK)
            continue
        values = tuple((value.name, "Done.") for value in page.required_values)
        page = service.complete(
            TASK, values, artifact="Done.", summary_for_next="Done."
        )
    return page


def _git_project(root: Path) -> None:
    def git(*arguments: str) -> None:
        subprocess.run(
            ["git", "-c", "commit.gpgsign=false", *arguments],
            cwd=root,
            check=True,
            capture_output=True,
        )

    git("init", "-q", "-b", "main", ".")
    git("config", "user.email", "t@e.st")
    git("config", "user.name", "Test")
    (root / ".gitignore").write_text(".ww/\n", encoding="utf-8")
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "seed")


def verification(root: Path) -> Instruction:
    """The verifier's page for a step's judged rule."""
    _git_project(root)
    service, _ = _start(root, RULES)
    service.next(TASK)
    (root / "notes.md").write_text("A change.\n", encoding="utf-8")
    page = service.complete(TASK, artifact="Developed.", summary_for_next="Done.")
    if page.item_status == "pending":
        page = service.next(TASK)
    return page


def fix_required(root: Path) -> Instruction:
    """ww rejected a completion: the checks that failed and how to go on."""
    _git_project(root)
    service, _ = _start(root, RULES)
    service.next(TASK)
    (root / "broken").write_text("", encoding="utf-8")
    return service.complete(TASK, artifact="First try.", summary_for_next="Done.")


SCENARIOS: dict[str, Callable[[Path], Instruction]] = {
    "step-single": step_single,
    "manager-dispatch": manager_dispatch,
    "worker-bootstrap": worker_bootstrap,
    "step-auto-worker": step_auto_worker,
    "worker-handoff": worker_handoff,
    "loop-body": loop_body,
    "loop-continue": loop_continue,
    "item-stage": item_stage,
    "values-and-metadata": values_and_metadata,
    "interactive": interactive,
    "step-rules": step_rules,
    "verification": verification,
    "fix-required": fix_required,
    "operator-stop": operator_stop,
    "completion": completion,
}


@contextmanager
def _fixed_tokens() -> Iterator[None]:
    with patch("ww.service.secrets.token_hex", return_value=TOKEN):
        yield


def render_page(name: str, root: Path) -> str:
    """Render one scenario's page in ``root``, with run-specific text replaced."""
    root.mkdir(parents=True, exist_ok=True)
    with _fixed_tokens():
        instruction = SCENARIOS[name](root)
    page = MarkdownOutputAdapter().render_instruction(instruction)
    for path in sorted({str(root.resolve()), str(root)}, key=len, reverse=True):
        page = page.replace(path, "<root>")
    page = _COMMIT.sub("<commit>", page)
    return _OUTPUT_DIRECTORY.sub("command-output/<id>/<id>/", page)


_COMMIT = re.compile(r"\b[0-9a-f]{40}\b")
_OUTPUT_DIRECTORY = re.compile(r"command-output/[0-9a-f]+/[0-9a-f]+/")


def render_pages(base: Path) -> dict[str, str]:
    """Every scenario's page, each rendered in its own directory under ``base``."""
    return {name: render_page(name, base / name) for name in SCENARIOS}


def approximate_tokens(text: str) -> int:
    """A rough token count: about four characters per token for English."""
    return (len(text) + 3) // 4


def measurements(pages: dict[str, str]) -> dict[str, dict[str, int]]:
    return {
        name: {"characters": len(page), "tokens": approximate_tokens(page)}
        for name, page in pages.items()
    }
