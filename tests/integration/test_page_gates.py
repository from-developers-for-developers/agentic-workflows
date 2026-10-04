# SPDX-License-Identifier: GPL-3.0-or-later
"""Every gate a page enforces still appears on it, however its wording changes.

The golden pages pin the exact text; this list pins what may never be cut
when that text is shortened: the command that records the work, what it must
carry, the checks, the loop and interaction commands, ww's handoff block, the
subagent ban, and the operator's stop.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.page_catalog import SCENARIOS, TOKEN, render_page

COMPLETE = "./ww complete TASK-1 --role worker"
ARTIFACT = '--artifact="<whole result in Markdown>"'
SUMMARY = '--summary="<one or two sentences for the next step>"'

GATES: dict[str, dict[str, tuple[str, ...]]] = {
    "step-single": {
        "completion command": (COMPLETE, ARTIFACT, SUMMARY),
        "no subagents": ("**No subagents.**", "`subagents: false`"),
        "single session": ("act as the worker", "not through a subagent"),
    },
    "manager-dispatch": {
        "manager command": ("./ww next TASK-1 --role manager",),
    },
    "worker-bootstrap": {
        "bootstrap": (
            f"./ww instruction TASK-1 --run 01-task --role worker --assignment {TOKEN}",
            "Pass only this command",
        ),
        "handoff block": ('"Handoff to manager" block', f"assignment `{TOKEN}`"),
    },
    "step-auto-worker": {
        "completion command": (
            f"{COMPLETE} --assignment {TOKEN}",
            ARTIFACT,
            SUMMARY,
        ),
        "worker": ("You are the worker", "write nothing under `.ww`"),
    },
    "worker-handoff": {
        "handoff block": (
            "### Handoff to manager",
            "Return the block below verbatim as your final message",
            "run no further `ww` command",
            f"```text\nHandoff to manager · assignment {TOKEN}",
            "Manager: continue with `./ww next TASK-1 --role manager`",
        ),
    },
    "loop-body": {
        "completion command": (COMPLETE, ARTIFACT, SUMMARY),
        "break": (
            "Break condition: There are no meaningful findings.",
            "./ww loop TASK-1 --break --role worker",
        ),
    },
    "loop-continue": {
        "completion command": (COMPLETE, ARTIFACT, SUMMARY),
        "continue": (
            "Continue condition: Findings remain for another round.",
            "./ww loop TASK-1 --continue --role worker",
        ),
    },
    "item-stage": {
        "completion command": (COMPLETE, ARTIFACT, SUMMARY),
        "item": ("./ww update-item TASK-1 --id comment-1",),
    },
    "values-and-metadata": {
        "completion command": (COMPLETE, ARTIFACT, SUMMARY),
        "provide": (
            '--variable priority="<priority>"',
            "- `priority` — The issue's priority",
        ),
        "metadata": (
            '--metadata jira.issue_id="<jira.issue_id>"',
            "`metadata.jira.issue_id`",
        ),
    },
    "interactive": {
        "completion command": (COMPLETE, ARTIFACT, SUMMARY),
        "interact": (
            "./ww interact TASK-1 --role worker --transcript - ",
            '--choice="<label or number>" --end',
            "<<'EOF'",
            "completion is refused while it is open",
            "clear contextual completion",
            "ask naturally whether they want to continue or finish",
            "`Done for today` means pause",
        ),
    },
    "step-rules": {
        "completion command": (COMPLETE, ARTIFACT, SUMMARY),
        "rules": ("`develop/1`", "under a **Rules** heading"),
        "checks": (
            "Checked automatically when you complete: `develop/sh`",
            "./ww check TASK-1",
        ),
    },
    "verification": {
        "completion command": (COMPLETE, ARTIFACT, "--rule-result="),
        "rules": ("`develop/1`", "#### What to report"),
    },
    "fix-required": {
        "completion command": (COMPLETE, ARTIFACT, SUMMARY),
        "checks": ("## Fix required", "`develop/sh`", "./ww check TASK-1"),
        "dispute": ("./ww dispute TASK-1 --role worker",),
    },
    "operator-stop": {
        "operator stop": (
            "`operator_reason: handler_failed`",
            "Stop and ask them",
            "do not pick for them",
            "./ww next TASK-1 --retry --yes --role manager",
            './ww next TASK-1 --force --reason "<reason>" --yes --role manager',
            "explicit approval",
        ),
    },
    "completion": {
        "workflow complete": ("## Manager: workflow complete",),
    },
}


def test_every_page_has_its_gates() -> None:
    assert set(GATES) == set(SCENARIOS)


@pytest.mark.parametrize(
    ("name", "gate"),
    [(name, gate) for name, gates in GATES.items() for gate in gates],
)
def test_the_gate_appears_on_its_page(tmp_path: Path, name: str, gate: str) -> None:
    page = render_page(name, tmp_path / name)

    missing = [text for text in GATES[name][gate] if text not in page]

    assert not missing, f"{name}: gate {gate!r} lost {missing}"


@pytest.mark.parametrize("name", ["worker-handoff", "operator-stop", "completion"])
def test_a_page_that_ends_the_turn_offers_no_completion(
    tmp_path: Path, name: str
) -> None:
    assert COMPLETE not in render_page(name, tmp_path / name)
