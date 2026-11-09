# SPDX-License-Identifier: GPL-3.0-or-later
"""The manager's page for a step whose agent ww runs headless."""

from __future__ import annotations

from ww.agents import FOREGROUND, WAIT_VARIABLE
from ww.instructions.commands import next_command
from ww.instructions.models import HeadlessView, Instruction


def render_headless(instruction: Instruction, view: HeadlessView) -> list[str]:
    """The page's lines: what ww did with the step's agent, and what next."""
    step = f"`{instruction.step or instruction.item_name}`"
    agent = f"`{view.agent}`"
    if view.state == "failed":
        return [
            f"## Manager: the headless run of {step} ended without completing it",
            "",
            *_failed(view, agent),
        ]
    if view.state == "refused":
        return [f"## Manager: ww did not start {agent} for {step}", "", *_refused(view)]
    if view.state == "queued":
        return [
            f"## Manager: {agent} waits for room to run headless for {step}",
            "",
            f"{agent} already runs as many headless runs as its `max_parallel` "
            "allows. Waiting starts this one as soon as one of them ends:",
            "",
            *wait_lines(instruction, view.wait_command),
        ]
    if view.state == "not_started":
        return [
            f"## Manager: ww runs {agent} headless for {step}",
            "",
            "ww starts this step's agent itself; start it with:",
            "",
            "```console",
            next_command(instruction.task_id),
            "```",
        ]
    return [
        f"## Manager: ww runs {agent} headless for {step}",
        "",
        *_running(instruction, view, agent),
    ]


def wait_lines(instruction: Instruction, wait_command: str) -> list[str]:
    """How the manager waits for headless runs, as its host can."""
    mechanism = instruction.host.waiting if instruction.host else FOREGROUND
    command = wait_command
    if mechanism.wait_seconds is not None:
        command = f"{WAIT_VARIABLE}={mechanism.wait_seconds} {command}"
    if mechanism.background:
        how = (
            "Run it in the background with your shell tool's background option "
            "and go on with the conversation: you may keep talking to the "
            "operator, and its output reaches you when it returns."
        )
    else:
        how = (
            "It blocks while it waits. If you would rather not block, tell the "
            "operator the run is going and end your turn; when they write "
            "again, run it then."
        )
    return [
        "```console",
        command,
        "```",
        "",
        how,
        "",
        "The command prints your next page once no run is going, or this page "
        "again after a while if one still is: then run it again. Until then, do "
        "not run commands that change this task; `status` and `instruction` "
        "are fine.",
    ]


def _running(instruction: Instruction, view: HeadlessView, agent: str) -> list[str]:
    return [
        f"ww started {agent} (launch `{view.launch_id}`, {view.started_at}) on "
        "this step, with the ordinary worker bootstrap: it performs the step "
        "and records its own result. You do not perform or delegate it.",
        "",
        "Wait for it:",
        "",
        *wait_lines(instruction, view.wait_command),
        "",
        f"The agent's output is logged in `{view.output_log}`.",
    ]


def branch_lines(view: HeadlessView) -> list[str]:
    """One headless branch's state and, after a failure, its choices."""
    agent = f"`{view.agent}`"
    if view.state == "running":
        return [
            f"- Headless: ww runs {agent} (launch `{view.launch_id}`, "
            f"{view.started_at}); log `{view.output_log}`"
        ]
    if view.state == "queued":
        return [f"- Headless: {agent} waits for room under its `max_parallel`"]
    if view.state == "not_started":
        return [f"- Headless: ww starts {agent} when you run `{view.wait_command}`"]
    if view.state == "refused":
        return [
            f"- Headless: ww did not start {agent}:",
            *(f"  - {finding}" for finding in view.findings),
            f"- Then: `{view.relaunch_command}` once fixed, or "
            f"`{view.session_command}` to give the branch to a subagent",
        ]
    choices = [
        *([f"`{view.resume_command}`"] if view.resume_command else []),
        f"`{view.relaunch_command}`",
        f"`{view.session_command}`",
    ]
    return [
        f"- Headless: {agent} (launch `{view.launch_id}`) {view.reason}; "
        f"logs `{view.output_log}` and `{view.error_log}`",
        "- Then, once you know why: " + ", or ".join(choices),
    ]


def _refused(view: HeadlessView) -> list[str]:
    return [
        "The agent's program failed the checks that must hold before ww starts "
        "it, so nothing was started:",
        "",
        *(f"- {finding}" for finding in view.findings),
        "",
        "Fix what the errors name (`ww agents --check` shows the program's "
        "state), then start it:",
        "",
        "```console",
        view.relaunch_command,
        "```",
        "",
        "Or take the step back into a session, to perform or delegate as usual:",
        "",
        "```console",
        view.session_command,
        "```",
        "",
        "If neither is yours to decide, stop and ask the operator.",
    ]


def _failed(view: HeadlessView, agent: str) -> list[str]:
    session = f"`{view.session_id}`" if view.session_id else "none named"
    lines = [
        f"ww started {agent} on this step (launch `{view.launch_id}`, "
        f"{view.started_at}), and {view.reason}.",
        "",
        f"- Exit code: {view.exit_code if view.exit_code is not None else 'none'}",
        f"- Agent session: {session}",
    ]
    if view.usage is not None and view.usage.total:
        lines.append(
            f"- Tokens: {view.usage.input} input, {view.usage.output} output, "
            f"{view.usage.cache_read} cache read, {view.usage.cache_write} cache write"
        )
    lines += [
        f"- Output log: `{view.output_log}`",
        f"- Error log: `{view.error_log}`",
    ]
    for title, tail in (
        ("output", view.output_tail),
        ("error output", view.error_tail),
    ):
        if tail:
            lines += ["", f"The end of its {title}:", "", "```text", *tail, "```"]
    lines += [
        "",
        "Find out what went wrong before you choose: read the logs, and check "
        "the workspace with `git status` and `git diff` for changes the run "
        "left half done. Then do one of these:",
        "",
    ]
    if view.resume_command is not None:
        lines += [
            "- Continue the same agent session, which keeps what it already "
            "knows (usually best after a timeout or an interruption):",
            "",
            "  ```console",
            f"  {view.resume_command}",
            "  ```",
            "",
        ]
    lines += [
        "- Start the agent again from the beginning, for example after cleaning "
        "up its half-done changes:",
        "",
        "  ```console",
        f"  {view.relaunch_command}",
        "  ```",
        "",
        "- Take the step back into a session and perform or delegate it as usual:",
        "",
        "  ```console",
        f"  {view.session_command}",
        "  ```",
        "",
        "- Stop and ask the operator, when the cause is theirs to fix (a login "
        "that expired, a missing program, a spent budget) or the choice is "
        "theirs.",
    ]
    return lines
