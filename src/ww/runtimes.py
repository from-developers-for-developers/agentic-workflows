# SPDX-License-Identifier: GPL-3.0-or-later
"""Workflow-runtime definitions for agent orchestration."""

from ww.contracts import NextRole
from ww.errors import ConfigurationError
from ww.executable import ww_command

RUNTIME_INSTRUCTIONS = {
    "single": (
        "Use this session for both manager and worker responsibilities. The "
        "manager dispatches an assignment; the worker completes its action and "
        "associated hooks until ww hands control back.",
        "Do not spawn subagents.",
        "Configured agent, model, reasoning, profile, skill, and built-in hints "
        "are ignored. The user-controlled session settings are authoritative.",
    ),
    "auto": (
        "The manager dispatches each assignment to a worker and handles recovery.",
        "You may use one worker for every step, choose a fresh worker per "
        "step or assignment, or perform the assignment yourself.",
        "Launch each worker with the model and reasoning the assignment "
        "requests; they take precedence over profile or skill preferences. "
        "Where none are shown, launch it with its default settings and do not "
        "choose them yourself.",
        "If the exact request is unavailable, select the closest worker and "
        "report the difference. Agent hints are advisory and currently unenforced.",
        "The worker submits its own results and associated hook results with "
        "--role worker until ww explicitly hands control back. It then "
        'returns ww\'s "Handoff to manager" block verbatim as its final '
        "message; the manager reads the outcome there.",
        "A worker may delegate one bounded hook when its runtime permits, but "
        "that worker remains responsible for submitting the result. Nested "
        "workers must not complete the same item or run manager commands.",
        "Use this runtime when the work needs execution settings this session "
        "cannot change, because a worker can be launched with them.",
    ),
}

# One-line summaries for choosing a runtime; ``RUNTIME_INSTRUCTIONS`` holds the
# guidance repeated in every instruction.
RUNTIME_DESCRIPTIONS = {
    "single": (
        "One session plays both manager and worker and does every assignment "
        "itself, without subagents."
    ),
    "auto": (
        "The manager delegates each assignment to a worker agent it selects, "
        "using the requested agent, model, and reasoning."
    ),
}
DEFAULT_RUNTIME = "single"


def requested_setting(value: object) -> str | None:
    """A requested model or reasoning, or ``None`` when the workflow asks for none.

    ``auto`` is not shown to agents: an open choice invites them to make one,
    so an unrequested setting is omitted and the worker keeps its default.
    """
    return value if isinstance(value, str) and value != "auto" else None


def cli_ownership_warning() -> str:
    """The strict reminder every runtime instruction ends with."""
    ww = ww_command()
    return (
        f"Strict: only {ww} start, next, and complete operate this flow. Do not "
        "mimic or bypass it with direct commands, or read "
        "ww.yaml; follow only the ww execution plan."
    )


def runtime_instruction(
    name: str, next_role: NextRole | None = None
) -> tuple[str, ...]:
    """Return a validated instruction for a persisted workflow runtime."""
    try:
        instructions = RUNTIME_INSTRUCTIONS[name]
        role_instruction: tuple[str, ...]
        if next_role == "worker":
            role_instruction = (
                "Worker responsibility: perform the active assignment and use "
                "the displayed --role worker completion command. Continue its "
                "associated hooks until ww reports a manager handoff.",
            )
        elif next_role == "manager":
            role_instruction = (
                "Manager responsibility: dispatch the next assignment or handle "
                "the displayed recovery action. Do not ask a worker to run a "
                "manager command.",
            )
        elif next_role == "operator":
            role_instruction = (
                "Operator decision: ww waits for the operator, the user. Stop "
                "and report the situation to them; run a displayed recovery "
                "command only after they choose it.",
            )
        else:
            role_instruction = ()
        return (*instructions, *role_instruction, cli_ownership_warning())
    except KeyError as error:
        supported = ", ".join(sorted(RUNTIME_INSTRUCTIONS))
        raise ConfigurationError(
            f"unsupported workflow runtime {name!r}; choose one of: {supported}"
        ) from error
