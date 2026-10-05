# SPDX-License-Identifier: GPL-3.0-or-later
"""``lookup``: which task a direct change belongs to, and what to do next.

An agent about to change files outside any workflow runs ``lookup`` with what
the operator called the task, or with nothing when they named none. ww maps
the reference onto the project's task format and existing tasks and answers
with one next step: continue an unfinished run, or work directly and register
it afterwards with ``record``. Only a reference that matches several tasks
asks the operator.

``lookup`` is read-only; the command it prints does the work.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from ww.agents import choice_mechanism
from ww.discovery import normalize_agent
from ww.errors import StateError
from ww.extensions import ExtensionRegistry
from ww.hooks.notices import (
    recent_interruptions_pointer,
)
from ww.hooks.records import HookRecords
from ww.instructions.commands import (
    TASK_PLACEHOLDER,
    instruction_command,
    record_command,
)
from ww.storage import Storage
from ww.storage_adapters import TaskStorageAdapter
from ww.task_ids import (
    EXPLICIT_TASK_FORMAT,
    candidate_task_ids,
    task_id_claimed,
)
from ww.task_references import resolve_task_reference

# Under ``"enabled": "on_request"`` lookup runs only because the user asked
# for ww; an unasked change is made without it, and without asking.
ON_REQUEST_NOTE = (
    "ww is used here only on request. Go on only if the user explicitly asked "
    "for ww; otherwise make the change without ww and do not ask."
)
DIRECT_NOTE = (
    "Work directly, as you would in a plain conversation, with no workflow "
    "and no question to the operator, then register what you did. If the "
    "registration fails or is forgotten, ww records the commits it had not "
    "seen on the next run; do not retry it endlessly."
)


@dataclass(frozen=True)
class _Choice:
    label: str
    description: str
    command: str

    def to_dict(self) -> dict[str, object]:
        return {
            "label": self.label,
            "description": self.description,
            "command": self.command,
        }


def lookup(
    storage: Storage,
    extensions: ExtensionRegistry,
    tasks: TaskStorageAdapter,
    reference: str | None,
    agent: str,
) -> dict[str, object]:
    """Resolve ``reference`` for a direct change and choose the next step."""
    normalize_agent(agent)
    del storage
    task_format = extensions.task_format()
    known = tasks.task_ids()
    if reference is not None and "/" in reference and tasks.task_exists(reference):
        # A child task is named by its full ID; top-level listing leaves it out.
        known = (*known, reference)
    resolution = (
        resolve_task_reference(reference, task_format, known)
        if reference is not None
        else None
    )
    report: dict[str, object] = {
        "on_request": extensions.config.on_request,
        "reference": reference,
        "matches": [
            _task(tasks, task_id)
            for task_id in (resolution.matches if resolution else ())
        ],
    }
    if resolution is not None and resolution.resolved is not None:
        return {**report, **_known_task(tasks, resolution.resolved, reference)}
    if resolution is not None and resolution.matches:
        choices = [
            _Choice(
                f"Use {task_id}",
                _describe(tasks, task_id),
                _continue_or_record(tasks, task_id),
            )
            for task_id in resolution.matches
        ]
        mechanism = choice_mechanism(agent)
        return {
            **report,
            "outcome": "choose",
            "message": (
                f"`{reference}` matches several existing tasks. Ask the "
                "operator which one this change belongs to."
            ),
            "choice_mechanism": mechanism.name,
            "choice_instruction": mechanism.instruction,
            "choices": [choice.to_dict() for choice in choices],
        }
    if resolution is not None and resolution.proposed is not None:
        task_id = resolution.proposed
        message = f"No task matches `{reference}`; `{task_id}` is a new task."
    elif task_format == EXPLICIT_TASK_FORMAT:
        task_id = TASK_PLACEHOLDER
        message = (
            "The request names no task, and this project requires an explicit "
            "task ID: use the external ticket key the request or conversation "
            f"works on in place of `{TASK_PLACEHOLDER}`."
        )
    else:
        task_id = _new_task_id(tasks, extensions, task_format)
        message = (
            f"The request names no task; ww assigned one for this change: `{task_id}`."
        )
    return {
        **report,
        "outcome": "direct",
        "task_id": task_id,
        "message": f"{message} {DIRECT_NOTE}",
        "command": record_command(task_id),
    }


def _known_task(
    tasks: TaskStorageAdapter, task_id: str, reference: str | None
) -> dict[str, object]:
    open_run = _open_run(tasks, task_id)
    if open_run is not None:
        return {
            "outcome": "continue",
            "task_id": task_id,
            "message": (
                f"`{reference}` is task `{task_id}`, which has an unfinished "
                f"`{open_run['workflow']}` run. The change belongs to that "
                "run: continue it instead of working outside it."
            ),
            "command": instruction_command(task_id, role="manager"),
        }
    return {
        "outcome": "direct",
        "task_id": task_id,
        "message": f"`{reference}` is task `{task_id}`. {DIRECT_NOTE}",
        "command": record_command(task_id),
    }


def _new_task_id(
    tasks: TaskStorageAdapter,
    extensions: ExtensionRegistry,
    task_format: str | None,
) -> str:
    for candidate in candidate_task_ids(task_format):
        if not task_id_claimed(candidate, tasks=tasks, extensions=extensions):
            return candidate
    raise StateError("could not generate an unused task ID")


def _open_run(tasks: TaskStorageAdapter, task_id: str) -> dict[str, str] | None:
    run_id = tasks.active_execution_run(task_id)
    if run_id is None:
        return None
    run = next(run for run in tasks.execution_runs(task_id) if run.run_id == run_id)
    return {"run_id": run.run_id, "workflow": run.workflow, "status": run.status}


def _task(tasks: TaskStorageAdapter, task_id: str) -> dict[str, object]:
    runs = tasks.execution_runs(task_id)
    return {
        "task_id": task_id,
        "runs": len(runs),
        "last_workflow": runs[-1].workflow if runs else None,
        "open_run": _open_run(tasks, task_id),
    }


def _describe(tasks: TaskStorageAdapter, task_id: str) -> str:
    open_run = _open_run(tasks, task_id)
    if open_run is not None:
        return f"Existing task with an unfinished `{open_run['workflow']}` run."
    runs = tasks.execution_runs(task_id)
    if runs:
        return f"Existing task; its last run was `{runs[-1].workflow}`."
    return "Existing task with no runs yet."


def _continue_or_record(tasks: TaskStorageAdapter, task_id: str) -> str:
    if _open_run(tasks, task_id) is not None:
        return instruction_command(task_id, role="manager")
    return record_command(task_id)


def render_lookup(
    storage: Storage,
    extensions: ExtensionRegistry,
    tasks: TaskStorageAdapter,
    reference: str | None,
    agent: str,
    json_output: bool,
) -> str:
    report = lookup(storage, extensions, tasks, reference, agent)
    days = extensions.config.agent_hooks.recent_days
    report["interrupted_recently"] = len(HookRecords(storage, tasks).recent(days))
    if json_output:
        return json.dumps(report, indent=2)
    return "\n".join(_markdown(report, days))


def _markdown(report: dict[str, object], days: int) -> list[str]:
    lines = ["# ww lookup", ""]
    if report.get("on_request"):
        lines.extend([f"**{ON_REQUEST_NOTE}**", ""])
    lines.append(str(report["message"]))
    count = report.get("interrupted_recently")
    pointer = recent_interruptions_pointer(count if isinstance(count, int) else 0, days)
    if pointer:
        lines.extend(["", pointer])
    command = report.get("command")
    if isinstance(command, str):
        lead = (
            ["When the work is done, register it:", ""]
            if report.get("outcome") == "direct"
            else []
        )
        return [*lines, "", *lead, "```console", command, "```"]
    choices = report["choices"]
    if not isinstance(choices, list):  # pragma: no cover - built above
        raise StateError("lookup choices are missing")
    lines.extend(["", "### Ask the operator", "", str(report["choice_instruction"])])
    lines.append("")
    lines.extend(
        f"{index}. **{choice['label']}** — {choice['description']}"
        for index, choice in enumerate(choices, start=1)
    )
    lines.extend(["", "### After the answer", ""])
    for choice in choices:
        lines.extend(
            [
                f"- **{choice['label']}**:",
                "",
                "  ```console",
                f"  {choice['command']}",
                "  ```",
            ]
        )
    lines.extend(
        [
            "",
            "Run a command only after the operator picked its choice, and follow "
            "each ww response from there. A `record` command comes after the "
            "work it registers.",
        ]
    )
    return lines
