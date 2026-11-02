# SPDX-License-Identifier: GPL-3.0-or-later
"""How an agent talks to the operator beyond plain text.

A workflow declares operator ``choices`` abstractly, and some stages make the
agent wait for the operator; how an agent presents choices with the keyboard,
and whether it can wait in the background, depends on the agent.  The neutral
mechanisms here are the fallbacks every agent can honour.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ChoiceMechanism:
    """How one agent presents a set of choices to the operator."""

    name: str
    instruction: str


PLAIN_TEXT = ChoiceMechanism(
    "plain text",
    "Present the choices as a numbered list in your reply, exactly in this "
    "order, and ask the operator to answer with the number or the label.",
)

# Question-tool availability varies by host and session, so each instruction
# ends in the fallback every agent can honour.
_FALLBACK = (
    " If the tool is not available in this session, present the choices as a "
    "numbered list instead and ask the operator to answer with the number or "
    "the label."
)


def question_tool(tool: str, picks: str) -> ChoiceMechanism:
    """Choices asked through the agent's question tool named ``tool``.

    ``picks`` says how the operator answers in that tool.
    """
    return ChoiceMechanism(
        tool,
        f"Present the matter in your reply first, then ask with the `{tool}` "
        "tool: one short question of a line or two, never the matter itself, "
        "these options in this order with their descriptions, single select. "
        f"{picks}" + _FALLBACK,
    )


@dataclass(frozen=True)
class WaitMechanism:
    """How one agent runs a command that waits for the operator.

    An agent with a background shell keeps its session free while the wait
    runs and hears the result when the command returns; such a wait may be
    long.  Any other agent blocks on the command, so the wait stays under
    its shell timeout.
    """

    background: bool
    wait_seconds: int | None
    instruction: str


FOREGROUND = WaitMechanism(
    False,
    None,
    "The command blocks until it returns; run nothing else while it waits, "
    "and run it again while items remain.",
)

# The environment variable that bounds one wait, and the bound a background
# wait gets: half an hour, long enough for a real session of manual testing,
# short enough that a forgotten wait ends on its own.
WAIT_VARIABLE = "WW_OPERATOR_WAIT"
BACKGROUND_WAIT_SECONDS = 1800
