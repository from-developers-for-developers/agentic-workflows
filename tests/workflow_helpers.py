# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared steps for tests that drive a workflow service."""

import json
from pathlib import Path
from typing import Any

from ww.instructions import Instruction
from ww.service import WorkflowService
from ww.storage import Storage


def configured_service(root: Path, configuration: str) -> WorkflowService:
    """A service over ``root`` once ``configuration`` is written as its ww.yaml."""
    (root / "ww.yaml").write_text(configuration, encoding="utf-8")
    return WorkflowService(Storage(root))


def run_state_path(root: Path, task_id: str, run_id: str = "01-task") -> Path:
    """The run document the task index currently names."""
    index = json.loads((root / ".ww/tasks" / task_id / "state.json").read_text())
    revision = next(
        entry["revision"] for entry in index["runs"] if entry["id"] == run_id
    )
    return root / ".ww/tasks" / task_id / "runs" / run_id / f"state.{revision}.json"


def start_after_init(
    service: WorkflowService,
    workflow: str,
    task_id: str | None,
    *args: Any,
    **kwargs: Any,
) -> Instruction:
    """Start a test workflow after recording its synthetic requirements step."""
    instruction = service.start(workflow, task_id, *args, **kwargs)
    return advance_init(service, instruction)


def advance_init(service: WorkflowService, instruction: Instruction) -> Instruction:
    """Complete a pending implicit init instruction in a service test."""
    if instruction.item_name == "init" and instruction.item_status == "pending":
        service.next(instruction.task_id)
        return service.complete(
            instruction.task_id,
            artifact="Recorded requirements.",
            summary_for_next="Done.",
        )
    return instruction


def start_child_after_init(
    service: WorkflowService, parent_id: str, child_id: str
) -> Instruction:
    return advance_init(service, service.start_child(parent_id, child_id))


def assignment_token(service: WorkflowService, task_id: str) -> str | None:
    """The open assignment's token, which the manager's pages hand a worker."""
    state, _ = service.load(task_id)
    return state.assignment_token
