#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Run a workflow to completion with deterministic placeholder agent results.

This is intentionally a smoke-test tool: it performs every ww-owned CLI action
for real, while treating prompt/skill/slash-command work as completed with a
small Markdown artifact.  Run it from a disposable branch when a workflow has
side effects such as ``git commit``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ww.config import load_configuration
from ww.errors import WwError
from ww.service import WorkflowService
from ww.storage import Storage


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_id")
    parser.add_argument("--workflow", required=True)
    parser.add_argument("--agent", default="codex")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--handoff-target",
        default=None,
        help="Workflow value used when a handoff requests `workflow`.",
    )
    args = parser.parse_args()
    root = args.root.resolve()
    service = WorkflowService(Storage(root))
    try:
        instruction = service.start(args.workflow, args.task_id, agent=args.agent)
        while instruction.status != "completed":
            if instruction.status == "failed":
                raise WwError(instruction.error or "automatic handler failed")
            if instruction.item_status == "pending":
                instruction = service.next(args.task_id)
                continue
            values = {
                value.name: _dummy_value(
                    value.name, args.handoff_target, root, instruction.workflow
                )
                for value in instruction.required_values
            }
            artifact = (
                f"# Dummy result\n\nCompleted `{instruction.item_name}` "
                "through the workflow smoke-test runner.\n"
            )
            instruction = service.complete(
                args.task_id,
                tuple(values.items()),
                artifact,
                summary_for_next="Dummy step completed.",
            )
    except WwError as error:
        print(f"ww smoke test failed: {error}", file=sys.stderr)
        return 2
    print(f"Workflow completed: {args.task_id} ({instruction.workflow})")
    return 0


def _dummy_value(
    name: str, handoff_target: str | None, root: Path, current_workflow: str
) -> str:
    if name == "workflow":
        if handoff_target:
            return handoff_target
        configuration = load_configuration(root / "ww-agentic-workflows.yaml")
        for workflow in configuration.workflows:
            if workflow.name != current_workflow and not workflow.handoff:
                return workflow.name
        raise WwError("handoff needs --handoff-target; no non-handoff workflow exists")
    return f"dummy-{name}"


if __name__ == "__main__":
    raise SystemExit(main())
