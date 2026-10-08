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
SUMMARY_FLAG = "--summary"


# A bare placeholder such as ``<run-id>``, or one shown already quoted such as
# ``"<reason>"`` so the agent keeps the quotes when it fills the value in;
# "<Run-ID>" does not match.
_PLACEHOLDER = re.compile(r'"?<[a-z][a-z0-9-]*>"?')


def _arg(value: str) -> str:
    """Quote a real value; keep a human placeholder such as ``<run-id>`` as is."""
    if _PLACEHOLDER.fullmatch(value):
        return value
    return shlex.quote(value)


def _command(*parts: str) -> str:
    return " ".join((ww_command(), *parts))


def _worker(role: CallerRole, assignment: str | None) -> tuple[str, ...]:
    """``--role`` and, for a worker of an open assignment, its token."""
    if role == "worker" and assignment:
        return ("--role", role, "--assignment", _arg(assignment))
    return ("--role", role)


def replan_command(task_id: str, *, keep: bool = False, rerun: bool = False) -> str:
    """The operator's choice at a ``plan_changed`` stop.

    Rerunning finished items is confirmed like a retry: ``--yes`` records
    that the operator agreed, since the agent's shell has no terminal.
    """
    if keep:
        return _command("next", _arg(task_id), "--keep-plan", "--role", "manager")
    flags = ("--replan", "--yes") if rerun else ("--replan",)
    return _command("next", _arg(task_id), *flags, "--role", "manager")


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
    """The manager command that advances a task, with optional recovery flags.

    A retry or force is the operator's choice, which the agent carries out
    only once they made it; ``--yes`` records that, since the agent's shell
    has no terminal to confirm at.
    """
    parts = ["next", _arg(task_id)]
    if retry:
        parts.append("--retry")
    if force_reason is not None:
        parts.extend(("--force", "--reason", _arg(force_reason)))
    if retry or force_reason is not None:
        parts.append("--yes")
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
    task_id: str,
    run_id: str | None = None,
    *,
    role: CallerRole,
    assignment: str | None = None,
) -> str:
    parts = ["instruction", _arg(task_id)]
    if run_id is not None:
        parts.extend(("--run", _arg(run_id)))
    parts.extend(_worker(role, assignment))
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
        "--requirements",
        '"<the request, normalized>"',
        "--role",
        "manager",
    )


def record_command(task_id: str = TASK_PLACEHOLDER) -> str:
    """Register direct work; the summary is the agent's to write."""
    return _command("record", _arg(task_id), SUMMARY_FLAG, '"<what was done>"')


def lookup_command(reference: str = "<task>", agent: str = "<agent>") -> str:
    return _command("lookup", _arg(reference), "--agent", _arg(agent))


def handoff_command(task_id: str, assignment: str | None = None) -> str:
    """Print the handoff block of an ended assignment again."""
    parts = ["handoff", _arg(task_id)]
    if assignment is not None:
        parts.extend(("--assignment", _arg(assignment)))
    return _command(*parts)


def status_command(task_id: str) -> str:
    """Show the task's state and result."""
    return _command("status", _arg(task_id))


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


def dispute_command(
    task_id: str, check_id: str = "<id>", *, assignment: str | None = None
) -> str:
    """Ask the operator to overrule a check that rejected the completion."""
    return _command(
        "dispute",
        _arg(task_id),
        *_worker("worker", assignment),
        "--rule",
        _arg(check_id),
        "--reason",
        '"<why the check is wrong here>"',
    )


def requirements_command(task_id: str) -> str:
    """Print the task's requirements and their amendments."""
    return _command("requirements", _arg(task_id))


def reset_command(task_id: str) -> str:
    """Remove a task's state, or an identity request, after confirmation."""
    return _command("reset", _arg(task_id), "--yes")


def start_child_command(task_id: str, child_id: str) -> str:
    return _command("start-child", _arg(task_id), _arg(child_id))


def update_child_command(task_id: str, child_id: str) -> str:
    return _command(
        "update-child", _arg(task_id), _arg(child_id), '--text="<child task text>"'
    )


def add_item_command(
    task_id: str = TASK_PLACEHOLDER, identity: str | None = None
) -> str:
    parts = ["add-item", _arg(task_id), "--id", "<id>", "--text", "<text>"]
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
    return _command("update-item", _arg(task_id), "--id", "<id>", "--text", "<text>")


def add_child_command(
    task_id: str = TASK_PLACEHOLDER, *, project: bool = False, identity: bool = False
) -> str:
    """The command that records one child; ``identity`` children take no ID."""
    return _command(
        "add-child",
        _arg(task_id),
        *(() if identity else ("--id", "<child-id>")),
        '--text="<child task text>"',
        *(("--project", "<project>") if project else ()),
    )


def items_command(task_id: str = TASK_PLACEHOLDER) -> str:
    return _command("items", _arg(task_id))


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
    assignment: str | None = None,
) -> str:
    """The command that submits a step or breaks/continues its loop.

    ``role`` is the manager only when the pending input belongs to an
    input-only assignment that the manager supplies itself. A verification
    item reports one ``--rule-result`` per rule ID in ``rule_results``.
    """
    parts = ["loop" if loop_control else "complete", _arg(task_id)]
    if loop_control:
        parts.append(f"--{loop_control}")
    parts.extend(_worker(role, assignment))
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
    return _command(*parts)


def interact_commands(
    task_id: str,
    role: CallerRole,
    assignment: str | None = None,
    *,
    choices: bool = False,
) -> InteractCommands:
    """The commands of an interactive step, for the role that performs it."""

    def interact(*parts: str) -> str:
        return _command("interact", _arg(task_id), *_worker(role, assignment), *parts)

    pick = ('--choice="<label or number>"',) if choices else ()
    transcript = "\n".join(
        [
            interact("--transcript", "-", *pick, "--end") + " <<'EOF'",
            "Agent: <what you said, verbatim>",
            "Operator: <what the operator said, verbatim>",
            "EOF",
        ]
    )
    return InteractCommands(
        transcript=transcript,
        operator=interact('--operator-said="<what the operator said>"'),
        agent=interact('--agent-said="<what the agent said>"'),
        choice=interact('--choice="<label or number>"'),
        end=interact("--end"),
        wait=interact("--await"),
        resume=instruction_command(task_id, role=role, assignment=assignment),
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
