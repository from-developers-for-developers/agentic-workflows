# SPDX-License-Identifier: GPL-3.0-or-later
"""The ``./ww`` command lines that instructions ask agents to run.

Every rendered command is built here, so a renamed flag changes in one place.
Real values are shell-quoted; ``<placeholder>`` tokens that a person or agent
must replace are left as written.
"""

from __future__ import annotations

import re
import shlex

from ww.contracts import CallerRole
from ww.executable import ww_command
from ww.workflow_config import ProvidedVariable, SavedMetadata

from .models import InteractCommands, RecoveryCommand

TASK_PLACEHOLDER = "<task-id>"
SUMMARY_FLAG = "--summary-for-next-step"


# A bare placeholder such as ``<run-id>``, or one shown already quoted such as
# ``"<reason>"`` so the agent keeps the quotes when it fills the value in.
_PLACEHOLDER = re.compile(r'"?<[a-z][a-z0-9-]*>"?')


def _arg(value: str) -> str:
    """Quote a real value; keep a human placeholder such as ``<run-id>`` as is."""
    if _PLACEHOLDER.fullmatch(value):
        return value
    return shlex.quote(value)


def _command(*parts: str) -> str:
    return " ".join((ww_command(), *parts))


def next_command(
    task_id: str,
    *,
    retry: bool = False,
    force_reason: str | None = None,
    selected_agent: str | None = None,
    model: str | None = None,
    reasoning: str | None = None,
    outcome: str | None = None,
) -> str:
    """The manager command that advances a task, with optional recovery flags."""
    parts = ["next", _arg(task_id)]
    if retry:
        parts.append("--retry")
    if force_reason is not None:
        parts.extend(("--force", "--force-reason", _arg(force_reason)))
    parts.extend(("--role", "manager"))
    for flag, value in (
        ("--selected-agent", selected_agent),
        ("--model", model),
        ("--reasoning", reasoning),
        ("--outcome", outcome),
    ):
        if value is not None:
            parts.extend((flag, _arg(value)))
    return _command(*parts)


def instruction_command(
    task_id: str, run_id: str | None = None, *, role: CallerRole
) -> str:
    parts = ["instruction", _arg(task_id)]
    if run_id is not None:
        parts.extend(("--run", _arg(run_id)))
    parts.extend(("--role", role))
    return _command(*parts)


def start_command(task_id: str | None, workflow: str, agent: str) -> str:
    """Start ``workflow``; without ``task_id`` ww assigns one."""
    return _command(
        "start",
        *((_arg(task_id),) if task_id is not None else ()),
        "--workflow",
        _arg(workflow),
        "--agent",
        _arg(agent),
        "--init-artifact",
        '"<the request, normalized>"',
        "--role",
        "manager",
    )


def lookup_command(reference: str = "<task>", agent: str = "<agent>") -> str:
    return _command("lookup", _arg(reference), "--agent", _arg(agent))


def artifacts_command(task_id: str, run_id: str | None = None) -> str:
    parts = ["artifacts", _arg(task_id)]
    if run_id is not None:
        parts.extend(("--run", _arg(run_id)))
    return _command(*parts)


def check_command(task_id: str) -> str:
    """Run the active step's checks now, without completing it."""
    return _command("check", _arg(task_id))


def rule_command(task_id: str, rule_id: str = "<id>") -> str:
    """Show one rule of the task in full."""
    return _command("rule", _arg(task_id), _arg(rule_id))


def dispute_command(task_id: str, check_id: str = "<id>") -> str:
    """Ask the operator to overrule a check that rejected the completion."""
    return _command(
        "dispute",
        _arg(task_id),
        "--rule",
        _arg(check_id),
        "--reason",
        '"<why the check is wrong here>"',
    )


def child_start_command(task_id: str, child_id: str) -> str:
    return _command("child", "start", _arg(task_id), _arg(child_id))


def add_item_command(
    task_id: str = TASK_PLACEHOLDER, identity: str | None = None
) -> str:
    parts = ["add-item", _arg(task_id), "--id", "<id>", "--item", "<text>"]
    if identity:
        parts.extend(("--field", f"{identity}=<value>"))
    return _command(*parts)


def set_item_fields_command(task_id: str, item_id: str | None = None) -> str:
    """Set custom fields on an item; several in one call."""
    return _command(
        "update-item",
        _arg(task_id),
        "--id",
        _arg(item_id) if item_id else "<id>",
        "--field",
        "<name>=<value>",
        "[--field <name>=<value> ...]",
    )


def remove_item_command(task_id: str = TASK_PLACEHOLDER) -> str:
    return _command("remove-item", _arg(task_id), "--id", "<id>")


def reword_item_command(task_id: str = TASK_PLACEHOLDER) -> str:
    return _command("update-item", _arg(task_id), "--id", "<id>", "--item", "<text>")


def add_child_command(
    task_id: str = TASK_PLACEHOLDER, *, project: bool = False, identity: bool = False
) -> str:
    """The command that records one child; ``identity`` children take no ID."""
    return _command(
        "add-child",
        _arg(task_id),
        *(() if identity else ("--id", "<child-id>")),
        '--description="<child task description>"',
        *(("--project", "<project>") if project else ()),
    )


def item_command(task_id: str, item_id: str) -> str:
    return _command("item", _arg(task_id), "--id", _arg(item_id))


_ITEM_UPDATE_FLAGS = {
    "process_item": ('--processed-item="<agent-friendly analysis>"',),
    "resolve_item": ('--actual-solution="<actual solution>"', "--resolved=true"),
    "report_item": ("--reported=true",),
    "handle_item": (
        '--processed-item="<agent-friendly analysis>"',
        '--actual-solution="<actual solution>"',
        "--resolved=true",
        "--reported=true",
    ),
}


def update_item_command(task_id: str, item_id: str, operation: str | None) -> str:
    """The update that saves progress for one item operation."""
    return _command(
        "update-item",
        _arg(task_id),
        "--id",
        _arg(item_id),
        *_ITEM_UPDATE_FLAGS.get(operation or "", ()),
    )


def complete_command(
    task_id: str,
    values: tuple[ProvidedVariable, ...],
    artifact: bool = True,
    metadata: tuple[SavedMetadata, ...] = (),
    *,
    selected_agent: str | None = None,
    selected_model: str | None = None,
    selected_reasoning: str | None = None,
    loop_control: str | None = None,
    role: CallerRole = "worker",
    summary: bool = False,
    rule_results: tuple[str, ...] = (),
    check_results: tuple[str, ...] = (),
) -> str:
    """The command that submits a step or breaks/continues its loop.

    ``role`` is the manager only when the pending input belongs to an
    input-only assignment that the manager supplies itself. A verification
    item reports one ``--rule-result`` per rule ID in ``rule_results`` and
    one ``--check-result`` per check name in ``check_results``.
    """
    parts = ["loop" if loop_control else "complete", _arg(task_id)]
    if loop_control:
        parts.append(f"--{loop_control}")
    parts.extend(("--role", role))
    parts.extend(f'--variable {value.name}="<{value.name}>"' for value in values)
    parts.extend(f'--metadata {value.name}="<{value.name}>"' for value in metadata)
    for flag, value in (
        ("--selected-agent", selected_agent),
        ("--selected-model", selected_model),
        ("--selected-reasoning", selected_reasoning),
    ):
        if value:
            parts.extend((flag, shlex.quote(value)))
    if artifact:
        parts.append('--artifact="<whole result in Markdown>"')
    if summary:
        parts.append(f'{SUMMARY_FLAG}="<one or two sentences for the next step>"')
    parts.extend(f"--rule-result='<JSON result for {rule}>'" for rule in rule_results)
    parts.extend(f"--check-result='<JSON check {name}>'" for name in check_results)
    return _command(*parts)


def interact_commands(task_id: str, role: CallerRole) -> InteractCommands:
    """The commands of an interactive step, for the role that performs it."""

    def interact(*parts: str) -> str:
        return _command("interact", _arg(task_id), "--role", role, *parts)

    return InteractCommands(
        operator=interact('--operator="<what the operator said>"'),
        agent=interact('--agent="<what the agent said>"'),
        choice=interact('--choice="<label or number>"'),
        end=interact("--end-interaction"),
        wait=interact("--await"),
        resume=instruction_command(task_id, role=role),
    )


def force_command(task_id: str) -> RecoveryCommand:
    """The operator's force: skip a failed item, or leave a loop at its limit."""
    return RecoveryCommand("force", next_command(task_id, force_reason='"<reason>"'))


def recovery_commands(task_id: str) -> tuple[RecoveryCommand, ...]:
    """The two operator choices for an interrupted automatic item."""
    return (
        RecoveryCommand("retry", next_command(task_id, retry=True)),
        force_command(task_id),
    )


def decision_commands(task_id: str, kind: str, key: str) -> tuple[RecoveryCommand, ...]:
    """The operator's choices for one proposal at a ``check_proposed`` stop.

    An approach is approved or replaced by the operator's own sentence, a
    check is approved, an ambiguous rule gets one of its readings picked.
    """
    def decide(*flags: str) -> str:
        return _command("next", _arg(task_id), *flags, "--role", "manager")

    if kind == "ambiguous":
        return (RecoveryCommand("pick", decide("--pick", f"{key}=<number>")),)
    commands = [RecoveryCommand("approve", decide("--approve", _arg(key)))]
    if kind == "approach":
        commands.append(
            RecoveryCommand(
                "approach", decide("--approach", _arg(key), '"<your approach>"')
            )
        )
    return tuple(commands)
