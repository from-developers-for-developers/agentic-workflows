# SPDX-License-Identifier: GPL-3.0-or-later
"""``lookup``: which task a catch-all change belongs to, and what to do next.

An agent about to change files outside any workflow runs ``lookup`` with what
the operator called the task, or with nothing when they named none. ww maps
the reference onto the project's task format and existing tasks and answers
with one next step: continue an unfinished run, start ``catchall`` on the task
found, or ask the operator. A task ww has never seen is only created after the
operator confirms it, through the agent's own choice menu.

``lookup`` is read-only; the command it prints does the work.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from ww.agents import choice_mechanism
from ww.builtin_workflows import CATCHALL
from ww.config import load_configuration
from ww.discovery import normalize_agent
from ww.errors import ConfigurationError, StateError
from ww.extensions import ExtensionRegistry
from ww.hooks.notices import (
    recent_interruptions_pointer,
)
from ww.hooks.records import HookRecords
from ww.instructions.commands import (
    TASK_PLACEHOLDER,
    instruction_command,
    start_command,
)
from ww.project_config import FILE_NAME
from ww.storage import Storage
from ww.storage_adapters import TaskStorageAdapter
from ww.task_ids import EXPLICIT_TASK_FORMAT
from ww.task_references import resolve_task_reference

WITHOUT_WW = "Work without ww"
# Under ``"enabled": "on_request"`` lookup runs only because the user asked
# for ww; an unasked change is made without it, and without asking.
ON_REQUEST_NOTE = (
    "ww is used here only on request. Go on only if the user explicitly asked "
    "for ww; otherwise make the change without ww and do not ask."
)


@dataclass(frozen=True)
class _Choice:
    label: str
    description: str
    # ``None`` for working without ww: no command follows that choice.
    command: str | None

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
    """Resolve ``reference`` for a catch-all change and choose the next step."""
    normalize_agent(agent)
    configuration = load_configuration(storage.config_path, extensions)
    catchall = configuration.workflows_by_name.get(CATCHALL)
    if catchall is None or catchall.manual:
        raise ConfigurationError(
            f"the {CATCHALL} workflow is switched off in {FILE_NAME}"
        )
    task_format = extensions.task_format()
    explicit = task_format == EXPLICIT_TASK_FORMAT
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
        task_id = resolution.resolved
        open_run = _open_run(tasks, task_id)
        if open_run is not None and open_run["workflow"] != CATCHALL:
            return {
                **report,
                "outcome": "continue",
                "task_id": task_id,
                "message": (
                    f"`{reference}` is task `{task_id}`, which has an unfinished "
                    f"`{open_run['workflow']}` run. The change belongs to that "
                    "run: continue it instead of starting another workflow."
                ),
                "command": instruction_command(task_id, role="manager"),
            }
        return {
            **report,
            "outcome": "start",
            "task_id": task_id,
            "message": (
                f"`{reference}` is task `{task_id}`. Record the change there: "
                f"start `{CATCHALL}` on it."
            ),
            "command": start_command(task_id, CATCHALL, agent),
        }
    if resolution is not None and resolution.matches:
        choices = [
            _Choice(
                f"Use {task_id}",
                _describe(tasks, task_id),
                _continue_or_start(tasks, task_id, agent),
            )
            for task_id in resolution.matches
        ]
        message = (
            f"`{reference}` matches several existing tasks. Ask the operator "
            "which one this change belongs to."
        )
        outcome = "choose"
    elif resolution is not None and resolution.proposed is not None:
        proposed = resolution.proposed
        choices = [
            _Choice(
                f"Create {proposed}",
                f"Start a new ww task `{proposed}` and record this change in it.",
                start_command(proposed, CATCHALL, agent),
            )
        ]
        message = (
            f"No task matches `{reference}`; ww has never seen `{proposed}`. "
            "Do not create it on your own: ask the operator to confirm."
        )
        outcome = "confirm"
    else:
        choices = [_new_task_choice(explicit, agent)]
        message = (
            "The request names no task. Do not create one on your own: ask the "
            "operator whether to record this change in a new task."
            if resolution is None
            else f"`{reference}` is not a valid task ID. Ask the operator "
            "whether to record this change in a new task."
        )
        outcome = "confirm"
    choices.append(
        _Choice(WITHOUT_WW, "Make the change without recording it in ww.", None)
    )
    mechanism = choice_mechanism(agent)
    return {
        **report,
        "outcome": outcome,
        "message": message,
        "choice_mechanism": mechanism.name,
        "choice_instruction": mechanism.instruction,
        "choices": [choice.to_dict() for choice in choices],
    }


def _new_task_choice(explicit: bool, agent: str) -> _Choice:
    if explicit:
        # The project never generates IDs; the operator types the key through
        # the menu's free-form answer, and the agent fills it in.
        return _Choice(
            "Create a task",
            "Start a new ww task under the ID the operator gives, such as a "
            "tracker key; ask for it if they did not type one.",
            start_command(TASK_PLACEHOLDER, CATCHALL, agent),
        )
    return _Choice(
        "Create a new task",
        "Start a new ww task, with an ID ww assigns, and record this change in it.",
        start_command(None, CATCHALL, agent),
    )


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


def _continue_or_start(tasks: TaskStorageAdapter, task_id: str, agent: str) -> str:
    open_run = _open_run(tasks, task_id)
    if open_run is not None and open_run["workflow"] != CATCHALL:
        return instruction_command(task_id, role="manager")
    return start_command(task_id, CATCHALL, agent)


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
        return [*lines, "", "```console", command, "```"]
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
        if choice["command"] is None:
            lines.append(
                f"- **{choice['label']}**: make the change directly, without "
                "any ww command."
            )
        else:
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
            "each ww response from there.",
        ]
    )
    return lines
