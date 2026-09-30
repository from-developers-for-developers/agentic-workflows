# SPDX-License-Identifier: GPL-3.0-or-later
"""Human-first rendering for normalized execution instructions."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import textwrap
from pathlib import Path

from ww.agents import WAIT_VARIABLE, choice_mechanism, wait_mechanism
from ww.assessments import AssessmentOutcome
from ww.children import ChildTask
from ww.config_files import SETTINGS_FILE
from ww.contracts import OperatorReason
from ww.executable import DEFAULT_EXECUTABLE, ww_command
from ww.instructions import Instruction, InteractCommands
from ww.instructions.commands import (
    add_item_command,
    artifacts_command,
    check_command,
    dispute_command,
    instruction_command,
    next_command,
    remove_item_command,
    reword_item_command,
    rule_command,
    set_item_fields_command,
    start_command,
)
from ww.instructions.models import FixFailure, Proposal, VerificationPage
from ww.instructions.policy import Audience, audience
from ww.output_adapters.base import OutputAdapter
from ww.output_adapters.terminal import initialization_progress, terminal_accent
from ww.results import NO_WORKFLOWS_ACTION, InitializationResult, ResetResult
from ww.runtimes import requested_setting

Lines = list[str]


class MarkdownOutputAdapter(OutputAdapter):
    def render_instruction(self, instruction: Instruction) -> str:
        if instruction.status == "task_summary":
            return _document(_task_summary(instruction))
        lines = _header(instruction)
        if instruction.status == "completed":
            _completed(lines, instruction)
            return _document(lines)
        if instruction.status == "abandoned":
            lines.extend(
                [
                    "## Run abandoned",
                    "",
                    f"Run `{instruction.run_id}` was abandoned by a later start of "
                    "the workflow and is kept as history. Nothing is to be done "
                    "in it; work on the task's current run.",
                ]
            )
            return _document(lines)
        if instruction.manager_only:
            _manager_only(lines, instruction)
            return _document(lines)
        if instruction.fix_required is not None and instruction.status != "failed":
            _fix_required(lines, instruction)
            _continuation(lines, instruction)
            return _document(lines)
        if _continues_assignment(instruction):
            return _document(_next_stage(lines, instruction))
        _heading(lines, instruction)
        _worker_selection(lines, instruction)
        _assignment_preview(lines, instruction)
        delegating = audience(instruction) is Audience.MANAGER_DELEGATING
        if delegating and instruction.role == "worker":
            _worker_bootstrap(lines, instruction)
            return _document(lines)
        _assignment_scope(lines, instruction)
        _working_directory(lines, instruction)
        _profile(lines, instruction)
        _task_requirements(lines, instruction)
        _previous_step_result(lines, instruction)
        _work(lines, instruction)
        _rules(lines, instruction)
        _verification(lines, instruction)
        _item_fields(lines, instruction)
        _stored_items(lines, instruction)
        _interaction(lines, instruction)
        _documents(lines, instruction)
        _run_handovers(lines, instruction)
        _input_context(lines, instruction)
        _loop_limit_recovery(lines, instruction)
        _next_steps(lines, instruction)
        _loop_outcome(lines, instruction)
        _previous_artifacts(lines, instruction)
        _error(lines, instruction)
        _failure(lines, instruction)
        _interrupted(lines, instruction)
        if instruction.artifact:
            lines.extend(["", f"Artifact: `{instruction.artifact}`"])
        _continuation(lines, instruction)
        return _document(lines)

    def render_reset(self, result: ResetResult) -> str:
        if result.removed:
            return (
                f"Task {result.task_id} was reset. Its state, plan, artifacts, "
                "and handoff marker were removed.\n"
            )
        return f"Task {result.task_id} was not found; nothing was removed.\n"

    def render_initialization(self, result: InitializationResult) -> str:
        lines = [
            f"{initialization_progress(100)} ww setup completed successfully in: "
            f"{result.root}"
        ]
        for title, entries in (
            ("Created or restored:", result.created),
            ("Already present and preserved:", result.preserved),
            (
                "Setup notes:",
                tuple(
                    action
                    for action in result.actions
                    if action != NO_WORKFLOWS_ACTION
                ),
            ),
        ):
            if entries:
                lines.extend(["", title, *(f"- {item}" for item in entries)])
        if _git_extension_active(result.root):
            lines.extend(
                [
                    "",
                    terminal_accent("Git support is enabled"),
                    "  The ww/git extension is configured in ww-agentic-workflows.json",
                    "  with its default settings. See:",
                    "  https://github.com/from-developers-for-developers/agentic-workflows/blob/main/documentation/features.md#configuring-one",
                ]
            )
        lines.append("")
        if result.permission_notice:
            lines.extend(_permission_notice(result.executable))
        # Getting started matters only until the first workflow exists.
        if NO_WORKFLOWS_ACTION in result.actions:
            lines.extend(
                [
                    terminal_accent("Next steps"),
                    "",
                    "  " + terminal_accent("1. Create your first workflow"),
                    "     Define the steps in ww-agentic-workflows.yaml.",
                    "",
                    "  " + terminal_accent("2. Start developing with your agent"),
                    "     For example, type:",
                    "",
                    "     /ww implement a user sign-in page",
                    "",
                    "  " + terminal_accent("Run commands manually"),
                    "     Use the project launcher for any ww command:",
                    "",
                    f"     {shlex.quote(result.executable)} workflows",
                    "",
                    *_initialization_shortcut(result.executable),
                ]
            )
        lines.extend(
            [
                terminal_accent("Documentation"),
                "",
                "  " + terminal_accent("README"),
                "    https://github.com/from-developers-for-developers/agentic-workflows",
                "",
                "  " + terminal_accent("Workflow specification"),
                "    https://github.com/from-developers-for-developers/agentic-workflows/blob/main/documentation/specification.md",
                "",
                "  " + terminal_accent("Examples"),
                "    https://github.com/from-developers-for-developers/agentic-workflows/blob/main/documentation/examples.md",
                "",
                "  " + terminal_accent("All features"),
                "    https://github.com/from-developers-for-developers/agentic-workflows/blob/main/documentation/features.md",
            ]
        )
        return _document(lines)


def _git_extension_active(root: str) -> bool:
    try:
        config = json.loads((Path(root) / SETTINGS_FILE).read_text())
        extensions = config.get("extensions", {})
        return isinstance(extensions, dict) and isinstance(
            extensions.get("ww/git"), dict
        )
    except (OSError, json.JSONDecodeError, AttributeError):
        return False


def _permission_notice(executable: str = DEFAULT_EXECUTABLE) -> Lines:
    """The one setup step that fails loudly later if it is skipped.

    Left as a trailing "tip" it was routinely missed, and the symptoms arrive
    much later looking unrelated: a confirmation prompt on every ww command,
    and an interactive step whose operator page cannot open a local port at
    all. It gets a boxed heading of its own, above the next steps, for that
    reason, and wraps to the terminal so the box never breaks.
    """
    width = max(44, min(72, shutil.get_terminal_size((80, 24))[0]))
    heading = "ACTION NEEDED — allow ww in your agent's permissions"
    rule = "─" * (width - 2)
    paragraphs = (
        "ww runs the commands your ww-agentic-workflows.yaml configures — your tests, "
        "linters, and commits — so an agent treats it as a command needing "
        "confirmation and asks every single time. Allow it once:",
        "Without this you get a prompt per step, and an interactive step's "
        "operator page cannot open its local port from inside an agent "
        "sandbox — it fails with a permission error rather than a busy port.",
        "What you are trusting is your own ww-agentic-workflows.yaml, with "
        "its local and machine-wide levels (ww-agentic-workflows.local.yaml "
        "here, ww-agentic-workflows.machine.yaml in your configuration "
        "directory): review changes to them like a CI config, since whoever "
        "edits them can run commands here.",
    )
    body = textwrap.wrap(paragraphs[0], width - 2, initial_indent="  ",
                         subsequent_indent="  ")
    lines: Lines = [
        terminal_accent(f"┌{rule}┐"),
        *(
            # Padded before it is coloured, so the escape codes never count
            # toward the width and the right edge always lines up.
            terminal_accent(f"│ {row.ljust(width - 4)} │")
            for row in textwrap.wrap(heading, width - 4)
        ),
        terminal_accent(f"└{rule}┘"),
        "",
        *body,
        "",
        f"     {executable}",
        "     ww   (when the shortcut exists)",
        "     ./ww",
        "",
    ]
    for paragraph in paragraphs[1:]:
        lines.extend(
            textwrap.wrap(paragraph, width - 2, initial_indent="  ",
                          subsequent_indent="  ")
        )
        lines.append("")
    return lines


def _initialization_shortcut(name: str) -> Lines:
    lines = ["  " + terminal_accent("Optional global shortcut")]
    executable = shutil.which(name)
    if executable:
        source = Path(executable).absolute()
        target = source.with_name("ww")
        if os.path.lexists(target):
            lines.extend(
                [f"     A ww shortcut or executable already exists: {target}", ""]
            )
            return lines
        lines.extend(
            [
                "     Link the installed executable as ww in the same PATH directory:",
                "",
                f"     ln -s {shlex.quote(str(source))} {shlex.quote(str(target))}",
            ]
        )
    else:
        lines.extend(
            [
                f"     After installing {name}, create a ww shortcut:",
                "",
                f"     ww_bin=$(command -v {shlex.quote(name)})",
                '     ln -s "$ww_bin" "$(dirname "$ww_bin")/ww"',
            ]
        )
    lines.extend(["", "     Then run: ww workflows", ""])
    return lines


def _document(lines: Lines) -> str:
    return "\n".join(lines) + "\n"


def _task_summary(instruction: Instruction) -> Lines:
    lines = [
        f"# {instruction.task_id}",
        "",
        "> Generated by `ww`. This is the authoritative status for this task.",
        "",
    ]
    if instruction.manager_intro:
        lines.extend(_manager_intro())
    lines.extend(["## Manager: choose a workflow run", ""])
    for run in instruction.task_runs:
        summary = f" — {run.summary}" if run.summary else ""
        lines.append(f"- `{run.run_id}` · `{run.workflow}` · {run.status}{summary}")
    lines.extend(
        [
            "",
            "Use `"
            + instruction_command(instruction.task_id, "<run-id>", role="manager")
            + "` for one run's detailed status.",
        ]
    )
    return lines


def _header(instruction: Instruction) -> Lines:
    lines = [
        f"# {instruction.task_id} · {instruction.workflow}",
        "",
        "> Generated by `ww`. This is the authoritative instruction for "
        "the current task state.",
    ]
    if instruction.completion_held and instruction.fix_required is None:
        lines.extend(
            [
                "> Completion accepted by `ww` and held: it is recorded once "
                "its rules are verified.",
                "",
            ]
        )
    elif (
        instruction.completion_registered
        and instruction.error is None
        and instruction.fix_required is None
        and instruction.status not in {"failed", "interrupted"}
    ):
        lines.extend(["> Completion recorded successfully by `ww`.", ""])
    else:
        lines.append("")
    if instruction.manager_intro:
        lines.extend(_manager_intro())
    return lines


def _completed(lines: Lines, instruction: Instruction) -> None:
    lines.extend(
        [
            "## Manager: workflow complete",
            "",
            "All agent work and automated handlers completed successfully.",
        ]
    )
    if instruction.handoff:
        lines.extend(["", f"Handoff: `{instruction.handoff}`"])
    if instruction.control == "handoff_manager":
        lines.extend(["", "Control is with the manager for final reporting."])
    _recommendation(lines, instruction)


def _recommendation(lines: Lines, instruction: Instruction) -> None:
    """Offer the recommended next workflow; only the operator may accept it."""
    workflow = instruction.recommended_workflow
    if workflow is None:
        return
    start = f"Start {workflow}"
    lines.extend(
        [
            "",
            "### Recommended next workflow",
            "",
            f"This workflow recommends `{workflow}` next, on the same task. Do "
            "not start it on your own: after your final report, ask the operator.",
            "",
            choice_mechanism(instruction.agent).instruction,
            "",
            f"1. **{start}** — Start the `{workflow}` workflow on "
            f"`{instruction.task_id}`.",
            "2. **Stop here** — End with this workflow and start nothing else.",
            "",
            f"If the operator picks **{start}**, run the command below and follow "
            "each ww response from there; otherwise stop.",
            "",
            "```console",
            start_command(instruction.task_id, workflow, instruction.agent),
            "```",
        ]
    )


def _heading(lines: Lines, instruction: Instruction) -> None:
    heading = f"{_role(instruction)}: {_action_heading(instruction)}"
    if instruction.item_status == "awaiting_input" and (
        audience(instruction) is not Audience.MANAGER_DELEGATING
    ):
        # A delegating manager keeps the "delegate the assignment" heading:
        # the worker it selects is the one who provides the input.
        heading = f"{_role(instruction)}: provide required input"
    if instruction.operator_reason is not None:
        heading = f"Operator decision: {_OPERATOR_REASONS[instruction.operator_reason]}"
    if instruction.operation_id and instruction.item_status == "in_progress":
        heading = "Manager: resolve the active automatic handler"
    lines.extend([f"## {heading}", ""])
    lines.extend(_awaiting_operator(instruction))
    lines.extend(_role_instruction(instruction))


_OPERATOR_REASONS: dict[OperatorReason, str] = {
    "handler_failed": "the automatic handler failed",
    "work_failed": "the step's work failed",
    "child_failed": "a child task failed",
    "interrupted_command": "an automatic handler was interrupted",
    "loop_limit": "the loop reached its iteration limit",
    "fix_limit": "the step's checks reached their fix limit",
    "check_proposed": "verifiers proposed checks for the step's rules",
    "check_disputed": "the step's worker disputed a check",
}


def _awaiting_operator(instruction: Instruction) -> Lines:
    """Say that ww now waits for the operator, to whoever may reach them."""
    if instruction.operator_reason is None or (
        instruction.workflow_runtime != "single"
        and instruction.caller_role == "worker"
    ):
        return []
    return [
        "`ww` is waiting for the operator, the user "
        f"(`operator_reason: {instruction.operator_reason}`). Stop and ask "
        "them: the recovery commands below are theirs to choose, and nothing "
        "runs until they do.",
        "",
    ]


def _worker_selection(lines: Lines, instruction: Instruction) -> None:
    if not (
        instruction.workflow_runtime == "auto"
        and instruction.next_role == "worker"
        and (
            instruction.requested_agent
            or requested_setting(instruction.requested_model)
            or requested_setting(instruction.requested_reasoning)
            or instruction.requested_profile
        )
    ):
        return
    lines.extend(
        [
            "Requested worker:",
            "",
            *(
                [f"- Agent: `{instruction.requested_agent}` (advisory)"]
                if instruction.requested_agent
                else []
            ),
            *requested_setting_lines(
                instruction.requested_model, instruction.requested_reasoning
            ),
            f"- Profile: `{instruction.requested_profile or 'none'}`",
            "",
        ]
    )
    if (
        instruction.selected_agent
        or instruction.selected_model
        or instruction.selected_reasoning
    ):
        lines.extend(
            [
                "Selected worker:",
                "",
                *(
                    f"- {label}: `{value}`"
                    for label, value in (
                        ("Agent", instruction.selected_agent),
                        ("Model", instruction.selected_model),
                        ("Reasoning", instruction.selected_reasoning),
                    )
                    if value
                ),
                "",
            ]
        )


def _assignment_preview(lines: Lines, instruction: Instruction) -> None:
    preview = instruction.assignment_preview
    if not preview or audience(instruction) is Audience.WORKER_RETURNING:
        return
    if "selection_item_name" in preview:
        lines.extend(
            [
                "",
                "### Upcoming assignment",
                "",
                f"Selection item: `{preview['selection_item_name']}`  ",
                f"Requested agent: `{preview['requested_agent']}`  ",
                *(
                    f"Requested {label}: `{value}`  "
                    for label, value in (
                        ("model", requested_setting(preview["requested_model"])),
                        (
                            "reasoning",
                            requested_setting(preview["requested_reasoning"]),
                        ),
                    )
                    if value is not None
                ),
                f"Requested profile: `{preview['requested_profile'] or 'none'}`",
            ]
        )
        scope = preview.get("item_scope") or preview.get("loop_scope")
        if isinstance(scope, dict):
            lines.extend(["", f"Scope: {_scope_summary(scope)}"])
    else:
        coordinator = "coordinator_item_id" in preview
        title = "Coordinator work" if coordinator else "Upcoming assignment"
        lines.extend(["", f"### {title}", "", str(preview["message"])])


def _scope_summary(scope: dict[str, object]) -> str:
    stages = ", ".join(f"`{name}`" for name in _strings(scope["stages"]))
    if "loop_assignment" in scope:
        return (
            f"one worker performs these steps ({stages}) of one round of the "
            f"`{scope['loop']}` loop."
        )
    item_ids = _strings(scope["item_ids"])
    if scope["item_assignment"] == "all_items":
        return (
            f"one worker performs every stage ({stages}) of all {len(item_ids)} items."
        )
    return f"one worker performs every stage ({stages}) of item `{item_ids[0]}`."


def _strings(value: object) -> list[str]:
    return [str(entry) for entry in value] if isinstance(value, list) else []


def _assignment_scope(lines: Lines, instruction: Instruction) -> None:
    scope = instruction.assignment_scope
    if scope is None or audience(instruction) is not Audience.WORKER:
        return
    _append_section(lines, "Assignment scope")
    unit = "step" if "loop_assignment" in scope else "stage"
    lines.extend(
        [
            f"In this assignment, {_scope_summary(scope)}",
            "",
            f"Do one {unit} at a time and complete each with its own worker "
            f"completion command; `ww` replies with the next {unit} straight "
            f"away. Do not start a later {unit} early. Keep going until `ww` "
            "says control returns to the manager.",
        ]
    )


def _continues_assignment(instruction: Instruction) -> bool:
    """A worker moving to the next stage of the item assignment it already holds."""
    return (
        instruction.continues_assignment
        and instruction.completion_registered
        and instruction.item_status == "in_progress"
        and audience(instruction) is Audience.WORKER
    )


def _next_stage(lines: Lines, instruction: Instruction) -> Lines:
    """The compact instruction for a later stage of the same assignment.

    The worker already holds the role, workspace, profile, and item or loop
    context, so only the new stage's work and its completion command are
    repeated.
    """
    lines.extend(
        [
            f"## Worker: next stage, `{instruction.item_name}`",
            "",
            "Continue in the same assignment.",
        ]
    )
    _work(lines, instruction)
    _rules(lines, instruction)
    _documents(lines, instruction)
    _loop_outcome(lines, instruction)
    _continuation(lines, instruction)
    return lines


def _worker_bootstrap(lines: Lines, instruction: Instruction) -> None:
    _append_section(lines, "Worker bootstrap")
    lines.extend(
        [
            "Pass only this command to the selected worker:",
            "",
            "```console",
            instruction_command(
                instruction.task_id,
                instruction.run_id,
                role="worker",
                assignment=instruction.assignment_token,
            ),
            "```",
            "",
            "The worker runs it to receive the complete, role-specific "
            "assignment. Do not add task details or commentary.",
        ]
    )


def _working_directory(lines: Lines, instruction: Instruction) -> None:
    if instruction.working_directory:
        _append_section(lines, "Working directory")
        lines.extend(
            [
                "Before doing any task work, switch to this task workspace "
                "and keep it as your current directory:",
                "",
                "```console",
                f"cd {shlex.quote(instruction.working_directory)}",
                "```",
            ]
        )


def _profile(lines: Lines, instruction: Instruction) -> None:
    if instruction.item_status == "in_progress" and instruction.profile_instruction:
        lines.extend(["", "### Profile", "", instruction.profile_instruction])


def _task_requirements(lines: Lines, instruction: Instruction) -> None:
    """Repeat the saved requirements, so the user's wording reaches the worker."""
    if instruction.item_status != "in_progress" or not instruction.task_requirements:
        return
    _append_section(lines, "Task requirements")
    lines.append(instruction.task_requirements)


def _previous_step_result(lines: Lines, instruction: Instruction) -> None:
    """Hand the previous step's summary and artifact reference to this step."""
    if instruction.item_status != "in_progress" or not instruction.previous_step:
        return
    _append_section(lines, "Previous step result")
    if instruction.previous_step_summary:
        lines.extend(
            [
                f"The `{instruction.previous_step}` step left this summary for you:",
                "",
                *_blockquote(instruction.previous_step_summary),
                "",
            ]
        )
    lines.append(
        f"Its full result is the artifact `{instruction.previous_step_artifact}`; "
        "read it when the summary is not enough."
    )


def _work(lines: Lines, instruction: Instruction) -> None:
    if not instruction.action_text:
        return
    _append_section(
        lines,
        "Loop limit reached" if instruction.loop_limit_reached else "Work instruction",
    )
    if instruction.ui and instruction.item_status == "in_progress":
        lines.extend(
            [
                "> **Operator page.** This stage is answered by the operator on "
                "the operator page and completed by ww from the answer. Do not "
                "present the item or ask the operator yourself; read the "
                "Operator page section below and run its command.",
                "",
            ]
        )
    lines.append(instruction.action_text)
    _loop_round(lines, instruction)
    _assessment_answers(lines, instruction)


def _rules(lines: Lines, instruction: Instruction) -> None:
    """The rules the step's worker follows, and those ww checks at completion."""
    if not instruction.rules or instruction.item_status != "in_progress":
        return
    _append_section(lines, "Rules")
    lines.extend(
        [
            "Follow these while working. ww checks them when you complete; run "
            f"`{check_command(instruction.task_id)}` at any time to see the "
            "result without completing. Read a full rule with "
            f"`{rule_command(instruction.task_id)}`.",
        ]
    )
    judged = [rule for rule in instruction.rules if not rule.has_command]
    checked = [rule for rule in instruction.rules if rule.has_command]
    if judged:
        lines.append("")
        for rule in judged:
            scope = f" — {', '.join(rule.paths)}" if rule.paths else ""
            lines.append(f"- `{rule.id}`{scope} — {rule.summary}")
            if rule.interpretation:
                lines.append(f"  {rule.interpretation}")
            if rule.pending_operator:
                lines.append(
                    "  This rule has a pending proposal; the operator has not "
                    "yet decided."
                )
        lines.extend(
            [
                "",
                "A verifier, not you, judges these rules against the files you "
                "changed after you complete.",
            ]
        )
    if checked:
        lines.extend(
            [
                "",
                "Checked automatically when you complete: "
                + ", ".join(f"`{rule.id}`" for rule in checked)
                + ".",
            ]
        )
    for reason, waived in _waivers(instruction.checks_waived):
        names = ", ".join(f"`{check_id}`" for check_id in waived)
        lines.extend(
            [
                "",
                f"The operator waived these checks for this step ({names}): "
                f"{reason}. ww does not run or verify them when you complete, "
                "and the artifact records the waiver.",
            ]
        )
    lines.extend(
        [
            "",
            "State in your artifact, under a **Rules** heading, which rules you "
            "applied and any deviation with its reason.",
        ]
    )


def _fix_required(lines: Lines, instruction: Instruction) -> None:
    """The page after ww rejected a completion: what failed, and how to go on."""
    fix = instruction.fix_required
    assert fix is not None
    failed = len(fix.failures)
    lines.extend(
        [
            f"## Fix required: {failed} of {fix.checks} checks failed "
            f"(attempt {fix.attempt} of {fix.max_fixes})",
            "",
            "ww did not record your completion: the checks below failed on the "
            "files this step changed. The step is still yours.",
        ]
    )
    _fix_failures(lines, fix.failures)
    lines.extend(
        [
            "",
            "Fix the causes, then complete again with a revised artifact. Your "
            "previous artifact is kept as a draft. Run "
            f"`{check_command(instruction.task_id)}` to see the checks' result "
            "before you complete.",
            "",
            "If a check is wrong for this change, fixing your work around it "
            "is not the answer: dispute it, with the evidence, and the "
            "operator decides whether it stands:",
            "",
            "```console",
            dispute_command(
                instruction.task_id, assignment=instruction.assignment_token
            ),
            "```",
        ]
    )


def _waivers(
    waived: tuple[tuple[str, str], ...],
) -> list[tuple[str, list[str]]]:
    """Waived check IDs grouped by the operator's reason, in waiver order."""
    reasons: dict[str, list[str]] = {}
    for check_id, reason in waived:
        reasons.setdefault(reason, []).append(check_id)
    return list(reasons.items())


def _manager_only(lines: Lines, instruction: Instruction) -> None:
    """A worker asked for an item the manager performs: no command for it."""
    why = (
        "is interactive: only the manager's session can talk to the operator"
        if instruction.interactive
        else "is the manager's (`role: manager`)"
    )
    lines.extend(
        [
            f"## `{instruction.item_name}` is the manager's",
            "",
            f"`{instruction.item_name}` {why}, so the manager performs it in "
            "its own session; a worker never does. Your assignment has ended: "
            "do not perform this item and run no further `ww` command. Return "
            "to the manager with your last `ww` response.",
        ]
    )


def _fix_failures(lines: Lines, failures: tuple[FixFailure, ...]) -> None:
    for failure in failures:
        if failure.hook:
            suffix = " (hook)"
        elif failure.judged:
            suffix = " (verifier's verdict)"
        elif failure.covers:
            suffix = " (check covering " + ", ".join(
                f"`{rule_id}`" for rule_id in failure.covers
            ) + ")"
        else:
            suffix = ""
        _append_section(lines, f"`{failure.id}`{suffix}")
        if failure.text:
            lines.extend([failure.text, ""])
        if failure.command:
            lines.append(f"Command: {failure.command}")
        output = failure.output.strip()
        if failure.judged:
            lines.append("The verifier found:")
        else:
            lines.append("Output:" if output else "Output: nothing")
        lines.extend(f"    {line}" for line in output.splitlines())


def _verification(lines: Lines, instruction: Instruction) -> None:
    """A verification item's rules, evidence, instructions, and result shape."""
    page = instruction.verification
    if page is None or instruction.item_status != "in_progress":
        return
    _append_section(lines, "Verification")
    lines.append(
        f"You verify the rules of the `{page.step}` step. Judge its work from "
        "the change set and the rules alone; do not change it."
    )
    if instruction.workflow_runtime == "single":
        lines.extend(
            [
                "",
                "Note: in the `single` runtime this session also did that "
                "step's work. Read the change as a reviewer would, not from "
                "memory of writing it.",
            ]
        )
    lines.extend(["", "#### Rules", ""])
    for rule in page.rules:
        lines.append(f"- `{rule.id}` — {_VERIFICATION_ASK[rule.state]}")
        lines.extend(f"  {line}" for line in rule.text.splitlines())
        if rule.interpretation:
            lines.append(f"  Interpretation: {rule.interpretation}")
        if rule.state == "approach-approved":
            lines.append(f"  Approved approach: {rule.approach}")
            lines.append(f"  Check to prepare: `{rule.check}`")
        if rule.pending_operator:
            lines.append(
                "  A proposal for this rule waits for the operator; judge it "
                "for now."
            )
    _verification_evidence(lines, page)
    _verification_duties(lines, page)
    _verification_results(lines, page)


_VERIFICATION_ASK = {
    "unresolved": "unresolved: interpret it and propose how to check it",
    "approach-approved": "approach approved: prepare and prove its check",
    "judged": "judged: give a verdict",
}


def _verification_evidence(lines: Lines, page: VerificationPage) -> None:
    lines.extend(["", "#### Change set", ""])
    if page.all_files:
        lines.append(
            "There is no git change set here: every file in the directory is "
            "in scope."
        )
    elif page.files:
        lines.append(f"Files the step changed ({len(page.files)}):")
        lines.append("")
        lines.extend(f"- `{path}`" for path in page.files)
    else:
        lines.append("The step changed no files.")
    if page.diff_command:
        lines.extend(
            ["", "Show the change itself:", "", "```console", page.diff_command, "```"]
        )
    if page.draft_artifact:
        lines.extend(
            [
                "",
                "The step worker's artifact, held until you finish: "
                f"`{page.draft_artifact}`",
            ]
        )
    lines.extend(["", "#### Existing checks", ""])
    if not page.checks:
        lines.append("The project has no derived checks yet.")
    for check in page.checks:
        lines.append(f"- `{check.name}` ({check.status}): `{check.command}`")
        if check.config:
            lines.append(
                "  Configuration: " + ", ".join(f"`{path}`" for path in check.config)
            )
        for text in check.covers:
            lines.append(f"  Covers: {text}")


def _verification_duties(lines: Lines, page: VerificationPage) -> None:
    states = {rule.state for rule in page.rules}
    lines.extend(["", "#### What to do", ""])
    if "unresolved" in states:
        lines.extend(
            [
                "- Interpret each rule in one sentence. If more than one "
                "reasonable reading exists, report `ambiguous` with the "
                "candidates and stop on that rule.",
                "- Propose the fewest checks for the unresolved rules; a check "
                "may cover several rules. Prefer the ecosystem's own tools (for "
                "PHP deptrac, PHPStan or Psalm rules; for Python import-linter, "
                "ruff or a pytest architecture test; for JavaScript eslint "
                "rules), because they express many rules in one configuration "
                "and run once. Before proposing a new check, look at the "
                "existing checks above: if one's tool can express the rule, "
                "propose extending its configuration, naming that check. Use "
                "plain shell (`grep`, `find`, `git`, `sed`, `awk`) only for "
                "what no tool covers. Write no command and install nothing "
                "yet: the operator approves the approach first.",
            ]
        )
    if "approach-approved" in states:
        lines.extend(
            [
                "- For a rule to prepare: install a tool into the project's "
                "manifest as a development dependency, never globally; keep its "
                "configuration in the repository and list those files under "
                "`config`. The command must run offline. Prove every check: "
                "construct a deliberately violating input in a temporary copy "
                "and show the command fails there and passes on the real change "
                "set. Report `not-convertible` with a reason when preparation "
                "shows the approach does not work.",
            ]
        )
    lines.append(
        "- For judged rules and for rules you could not convert: give a "
        "verdict `pass` or `fail`, with evidence `file:line — what` for each "
        "failure."
    )


def _verification_results(lines: Lines, page: VerificationPage) -> None:
    states = {rule.state for rule in page.rules}
    lines.extend(
        [
            "",
            "#### What to report",
            "",
            "Complete with your findings as the artifact and one "
            "`--rule-result` JSON object per rule:",
            "",
        ]
    )
    if "unresolved" in states:
        lines.extend(
            [
                '- an approach: `{"id": "<rule>", "interpretation": "<one '
                'sentence>", "status": "approach", "check": "<kebab-name>", '
                '"approach": "<the tool or command, one line>"}`',
                '- a reading to choose: `{"id": "<rule>", "status": "ambiguous", '
                '"candidates": ["<reading>", "<reading>"]}`',
            ]
        )
    if "approach-approved" in states:
        lines.append(
            '- a prepared check: `{"id": "<rule>", "status": "approach", '
            '"check": "<kebab-name>"}`'
        )
    if states & {"unresolved", "approach-approved"}:
        lines.append(
            '- not convertible: `{"id": "<rule>", "status": "not-convertible", '
            '"reason": "<why>", "verdict": "pass"}`'
        )
    lines.append(
        '- a verdict: `{"id": "<rule>", "status": "judged", "verdict": "fail", '
        '"failures": [{"file": "<path>", "line": 12, "what": "<what>"}]}`, '
        "or `\"verdict\": \"pass\"` without failures"
    )
    if page.prepares:
        lines.extend(
            [
                "",
                "And one `--check-result` per check you prepared: "
                '`{"name": "<kebab-name>", "argv": ["<program>", "<arg>"], '
                '"assert": null, "config": ["<path>"], "covers": ["<rule>"], '
                '"proven": true}`; a shell check gives `"shell"` (with '
                '`"args"` and `"env"`) instead of `"argv"`, and '
                '`"assert": {"operator": "empty"}` when it must print nothing.',
            ]
        )


def _check_proposed(lines: Lines, instruction: Instruction) -> None:
    """The operator's stop: what the verifiers proposed, and each decision."""
    worker = (
        instruction.workflow_runtime != "single" and instruction.caller_role == "worker"
    )
    lines.extend(
        [
            "",
            "The step's verifiers proposed how to check its rules. ww holds the "
            "step's completion until the operator decides every proposal "
            "below; nothing new runs before.",
        ]
    )
    for proposal in instruction.proposals:
        _proposal(lines, proposal, worker)
    if worker:
        lines.extend(
            [
                "",
                "Stop here. Return this `ww` response to the manager: the "
                "operator decides each proposal.",
            ]
        )
        return
    _append_section(lines, "Operator decision")
    lines.extend(
        [
            "Show the operator each proposal above, quoted, and ask for a "
            "decision on each; do not decide for them. An approved check runs "
            "at once on the held completion. To reject every proposal still "
            "undecided, which lets the completion go on with those rules "
            "judged, only with the operator's explicit approval:",
        ]
    )
    for command in instruction.recovery_commands:
        lines.extend(["", "```console", command.command, "```"])


def _proposal(lines: Lines, proposal: Proposal, worker: bool) -> None:
    title = {
        "approach": "an approach",
        "check": "a revision of an approved check"
        if proposal.revision
        else "a check",
        "ambiguous": "a reading to choose",
    }[proposal.kind]
    _append_section(lines, f"`{proposal.key}`: {title}")
    for rule_id, text in proposal.rules:
        lines.append(f"- `{rule_id}`: {text}")
    lines.append("")
    if proposal.interpretation:
        lines.append(f"Interpretation: {proposal.interpretation}")
    if proposal.kind == "approach":
        lines.append(f"Approach: {proposal.approach}")
        verb = "extend the existing check" if proposal.extends else "create the check"
        lines.append(f"It would {verb} `{proposal.check}`.")
    if proposal.kind == "check":
        lines.extend(["Command:", "", "```sh", str(proposal.command), "```", ""])
        if proposal.assertion:
            lines.append(f"Assertion: {proposal.assertion}")
        if proposal.config:
            lines.append(
                "Configuration files: "
                + ", ".join(f"`{path}`" for path in proposal.config)
            )
        lines.append(
            "Proof: the verifier showed it fails on a violation and passes on "
            "the change."
            if proposal.proven
            else "Proof: none; the verifier did not prove it."
        )
        if proposal.revision:
            lines.append(
                "The approved command keeps running until this revision is "
                "approved."
            )
    if proposal.kind == "ambiguous":
        lines.append("Readings:")
        lines.extend(
            f"{number}. {candidate}"
            for number, candidate in enumerate(proposal.candidates, 1)
        )
    if worker:
        return
    for command in proposal.commands:
        purpose = {
            "approve": "To approve it",
            "approach": "To replace the approach with the operator's own",
            "pick": "To choose a reading by its number",
        }[command.action]
        lines.extend(["", f"{purpose}:", "", "```console", command.command, "```"])


def _outcome_effect(outcome: AssessmentOutcome) -> str:
    if outcome.stops:
        return "ends the workflow here, skipping everything after it"
    if not outcome.declared:
        return (
            f"runs nothing extra and continues with `{outcome.first_step}`"
            if outcome.first_step is not None
            else "runs nothing extra and continues the workflow"
        )
    if outcome.first_step is not None:
        return f"continues with `{outcome.first_step}`"
    return "continues the workflow"


def _assessment_answers(lines: Lines, instruction: Instruction) -> None:
    if not instruction.assessment_outcomes or instruction.choosing_outcome_of:
        return
    _append_section(lines, "Possible outcomes")
    lines.append(
        "Your answer decides what runs next. Say plainly in the artifact which "
        "of these outcomes your assessment supports:"
    )
    lines.append("")
    lines.extend(
        f"- `{outcome.label}` — {_outcome_effect(outcome)}."
        for outcome in instruction.assessment_outcomes
    )


def _loop_round(lines: Lines, instruction: Instruction) -> None:
    """Tell a loop body step which round it is in and what that round covers."""
    if (
        instruction.loop_name is None
        or instruction.loop_iteration is None
        or instruction.is_loop_control
    ):
        return
    name = instruction.loop_name
    if instruction.loop_iteration <= 1:
        lines.extend(
            [
                "",
                f"This is the first round of the `{name}` loop. It builds on the "
                "work of the steps before the loop.",
            ]
        )
        return
    lines.extend(
        [
            "",
            f"This is round {instruction.loop_iteration} of the `{name}` loop, "
            f"limit {instruction.loop_max_times}. Concentrate on the work done "
            "in the previous rounds of this loop, not on the whole task. Their "
            "results are the artifacts in the loop's earlier iteration "
            "directories; list them with:",
            "",
            "```console",
            artifacts_command(instruction.task_id, instruction.run_id),
            "```",
        ]
    )


def _item_fields(lines: Lines, instruction: Instruction) -> None:
    """Custom item fields: what this step must set, and the flow's rules."""
    if instruction.item_status != "in_progress":
        return
    rules = instruction.item_identity or instruction.item_unique
    if not instruction.required_item_fields and not rules:
        return
    _append_section(lines, "Item fields")
    task_id = instruction.task_id
    if instruction.required_item_fields:
        target = (
            "every collected item" if instruction.collects_items else "this step's item"
        )
        lines.extend(
            [
                f"Set these custom fields on {target} before completing; "
                "completion is refused while any is empty. Several go in one "
                "command:",
                "",
            ]
        )
        lines.extend(
            f"- `{field.name}`"
            + (f" — {field.description}" if field.description else "")
            for field in instruction.required_item_fields
        )
        lines.extend(["", "```console", set_item_fields_command(task_id), "```"])
    if rules:
        lines.append("")
        if instruction.item_identity:
            lines.append(
                f"A new item must carry `{instruction.item_identity}`; `add-item` "
                "refuses one without it:"
            )
            command = add_item_command(task_id, instruction.item_identity)
            lines.extend(["", "```console", command, "```"])
        if instruction.item_unique:
            lines.extend(
                [
                    "",
                    "Across "
                    + ", ".join(f"`{n}`" for n in instruction.item_unique)
                    + " a value may appear once over all items, in this run and in "
                    "the task's stored items; ww refuses a duplicate and names the "
                    "item that holds it. Look an item up by a field with "
                    f"`{_command_by(task_id)}`.",
                ]
            )


def _command_by(task_id: str) -> str:
    return f"{ww_command()} item {task_id} --by <name>=<value>"


def _stored_items(lines: Lines, instruction: Instruction) -> None:
    """A shared item flow's collection: reconcile the stored items."""
    if instruction.item_status != "in_progress" or not instruction.shared_items:
        return
    _append_section(lines, "Stored items")
    task_id = instruction.task_id
    if not instruction.stored_items:
        lines.extend(
            [
                "This task shares its items across runs, and none are stored yet: "
                "split as instructed above. Every later run starts from the items "
                "you record now.",
            ]
        )
        return
    lines.extend(
        [
            "This task shares its items across runs. The items below were "
            "collected in an earlier run and this run starts from them, with "
            "their outcomes cleared. Do not split again: compare the source "
            "with this list and make the list match it, adding what is new, "
            "removing what is gone, and rewording what changed. Keep IDs "
            "stable, so an item that changed is reworded, not replaced. Remove "
            "an item only when it is gone from the source, never because it "
            "was done: outcomes are per run, and every run keeps its own copy. "
            "If nothing changed, complete the step as it is.",
            "",
        ]
    )
    lines.extend(
        f"- `{item.id}`: {item.item}"
        + (f" (refers to `{item.reference_to_id}`)" if item.reference_to_id else "")
        + (
            " [" + ", ".join(f"{k}={v}" for k, v in item.fields) + "]"
            if item.fields
            else ""
        )
        for item in instruction.stored_items
    )
    lines.extend(
        [
            "",
            "```console",
            add_item_command(task_id),
            remove_item_command(task_id),
            reword_item_command(task_id),
            "```",
        ]
    )


def _interaction(lines: Lines, instruction: Instruction) -> None:
    """The contract of an interactive step: talk, record both sides, end."""
    if instruction.item_status != "in_progress" or not instruction.interactive:
        return
    commands = instruction.interact_commands
    if commands is None:  # pragma: no cover - the builder sets them together
        raise ValueError("an interactive step needs its interact commands")
    if instruction.ui:
        _operator_page(lines, instruction, commands)
        return
    _append_section(lines, "Interaction with the operator")
    if instruction.interaction_ended:
        state = "The operator has ended this interaction; complete the step now."
    elif instruction.operator_paused:
        state = (
            "The operator is done for now. Stop here: do not complete the step, "
            "do not wait for them again, and do not delegate. The task keeps "
            "this state; when the operator returns, show this page with "
            f"`{commands.resume}` and go on."
        )
    elif instruction.interaction_entries:
        count = instruction.interaction_entries
        state = f"{count} entries recorded so far; the interaction is still open."
    else:
        state = "Nothing is recorded yet."
    lines.extend(
        [
            "This step is a conversation with the operator, held here in this "
            "session, because a delegated worker cannot talk to them. Present "
            "the matter, ask, listen, and clarify. The operator runs no `ww` "
            "command: read from their words whether the conversation goes on or "
            "is finished. Record both sides as you go, each message verbatim:",
            "",
            "```console",
            commands.operator,
            commands.agent,
            "```",
            "",
            "When the operator says it is finished, end the interaction, then "
            "complete the step as usual. Completion is refused while it is open.",
            "",
            "```console",
            commands.end,
            "```",
            "",
            state,
        ]
    )
    _choices(lines, instruction, commands.choice)
    _conversation(lines, instruction)


def _choices(lines: Lines, instruction: Instruction, choice: str) -> None:
    if not instruction.choices:
        return
    lines.extend(["", "#### Choices", ""])
    lines.extend(
        f"{number}. `{option.label}`"
        + (f" — {option.description}" if option.description else "")
        for number, option in enumerate(instruction.choices, 1)
    )
    lines.extend(
        [
            "",
            str(instruction.choice_mechanism),
            "",
            "Record the operator's pick before ending the interaction; it is "
            "required, and a comment they add goes in as `--operator` text:",
            "",
            "```console",
            choice,
            "```",
            "",
            (
                f"Chosen so far: `{instruction.chosen}`."
                if instruction.chosen
                else "Nothing chosen yet."
            ),
        ]
    )


def _operator_page(
    lines: Lines, instruction: Instruction, commands: InteractCommands
) -> None:
    """A stage answered on the operator page: the agent waits, ww applies."""
    _append_section(lines, "Operator page")
    if instruction.operator_paused:
        situation = (
            "The operator is done for now. Stop here: do not wait again and do "
            f"not delegate. When they return, show this page with "
            f"`{commands.resume}` and wait again."
        )
    else:
        situation = "Wait for the operator's answers:"
    mechanism = wait_mechanism(instruction.agent)
    command = commands.wait
    if mechanism.wait_seconds is not None:
        command = f"{WAIT_VARIABLE}={mechanism.wait_seconds} {command}"
    lines.extend(
        [
            "This stage is answered on the operator page, a local page that "
            "lists every item of the run. The operator answers each item there, "
            "in any order, with a pick and a comment. You do not talk to the "
            "operator for this stage and you do not complete it yourself: when "
            "the wait ends, ww applies the answers, completing this stage and "
            "every following item stage whose item was answered, in order, and "
            "the command prints what it applied and what remains, after the "
            "next page.",
            "",
            situation,
            "",
            "```console",
            command,
            "```",
            "",
            "The command returns when every item is answered, when the operator "
            "says they are done for now or closes the page, or after a while "
            "with nothing new. " + mechanism.instruction,
        ]
    )


def _conversation(lines: Lines, instruction: Instruction) -> None:
    if not instruction.conversation:
        return
    lines.extend(["", "#### Conversation so far", ""])
    for entry in instruction.conversation:
        lines.append(f"**{entry.speaker}** · {entry.at}")
        lines.extend(_blockquote(entry.text))
        lines.append("")
    while lines and lines[-1] == "":
        lines.pop()


def _documents(lines: Lines, instruction: Instruction) -> None:
    """Name the documents this step edits in place, the one exception under .ww."""
    if instruction.item_status != "in_progress" or not instruction.documents:
        return
    _append_section(lines, "Documents to update")
    lines.extend(
        [
            "Create or edit these files in place; they are the only files under "
            "`.ww` you may write. Keep whatever format the document already has. "
            "Each must exist when you complete, and `ww` then records that this "
            "step updated it.",
            "",
        ]
    )
    for document in instruction.documents:
        state = "exists" if document.exists else "does not exist yet; create it"
        lines.append(f"- `{document.name}` at `{document.path}` ({state})")
        if document.instruction:
            lines.append(f"  {document.instruction}")


def _run_handovers(lines: Lines, instruction: Instruction) -> None:
    """Give the workflow summary its inputs: each step's own handover."""
    if instruction.item_status != "in_progress" or not instruction.run_handovers:
        return
    _append_section(lines, "Step handovers of this run")
    lines.extend(
        [
            "Build the summary from these handovers, in order; each names its "
            "artifact for detail. State only what they, or the artifacts you "
            "actually read, contain: no counts, test results, or statuses from "
            "anywhere else, and nothing from other runs of this task.",
            "",
        ]
    )
    for handover in instruction.run_handovers:
        text = handover.summary or "(no handover recorded)"
        lines.append(f"- `{handover.step}`: {text} (artifact: `{handover.artifact}`)")


def _input_context(lines: Lines, instruction: Instruction) -> None:
    """Show a value-waiting handler the work its values are about."""
    if instruction.item_status != "awaiting_input" or not instruction.input_context:
        return
    handler = instruction.item_name or "this handler"
    _append_section(lines, "Work these values describe")
    lines.extend(
        [
            f"Steps completed since `{handler}` last ran in this run, in order. "
            "Base the values on this work only, not on earlier rounds or runs:",
            "",
        ]
    )
    for handover in instruction.input_context:
        text = handover.summary or "(no handover recorded)"
        lines.append(f"- `{handover.step}`: {text} (artifact: `{handover.artifact}`)")


def _loop_limit_recovery(lines: Lines, instruction: Instruction) -> None:
    """The operator's only way past a loop that reached its iteration limit."""
    if not instruction.loop_limit_reached or not instruction.recovery_commands:
        return
    if instruction.workflow_runtime != "single" and instruction.caller_role == "worker":
        return
    _append_section(lines, "Operator recovery")
    lines.append(
        "Wait for the decision of the user, who is the `ww` operator. If they "
        "resolve the remaining findings themselves, or accept them, run exactly "
        "this to leave the loop and continue with the steps after it:"
    )
    for command in instruction.recovery_commands:
        lines.extend(["", "```console", command.command, "```"])
    lines.extend(
        [
            "",
            "Do not run it without the operator's explicit approval. A further "
            "iteration needs a higher `loop_max_times` in the configuration.",
        ]
    )


def _next_steps(lines: Lines, instruction: Instruction) -> None:
    if instruction.next_steps:
        lines.extend(
            ["", "Avoid duplicating work that is better handled by the next steps."]
        )
        _append_section(lines, "Next steps")
        lines.extend(f"- {step}" for step in instruction.next_steps)


def _loop_outcome(lines: Lines, instruction: Instruction) -> None:
    if instruction.loop_break_prompt and instruction.loop_break_command:
        _append_section(lines, "Loop outcome")
        lines.extend(
            [
                f"Break condition: {instruction.loop_break_prompt}",
                "",
                "Condition met — break the loop:",
                "",
                "```console",
                instruction.loop_break_command,
                "```",
                "",
                "Condition not met — use the worker completion command below.",
            ]
        )
    if instruction.loop_continue_prompt and instruction.loop_continue_command:
        _append_section(lines, "Loop control")
        lines.extend(
            [
                f"Continue condition: {instruction.loop_continue_prompt}",
                "",
                "Condition met — continue from the beginning of the loop:",
                "",
                "```console",
                instruction.loop_continue_command,
                "```",
                "",
                "Condition not met — use the worker completion command below.",
            ]
        )


def _previous_artifacts(lines: Lines, instruction: Instruction) -> None:
    if instruction.item_status == "in_progress" and instruction.has_previous_artifacts:
        _append_section(lines, "Previous artifacts")
        lines.extend(
            [
                "You may use artifacts from earlier completed steps. List them "
                "as JSON with the command below; each entry's `path` is the "
                "absolute file location, valid from any working directory:",
                "",
                "```console",
                artifacts_command(instruction.task_id, instruction.run_id),
                "```",
            ]
        )


def _error(lines: Lines, instruction: Instruction) -> None:
    if instruction.error:
        _append_section(lines, "Error")
        lines.append(instruction.error)
        if instruction.result_saved is not None:
            saved = "was" if instruction.result_saved else "was not"
            lines.extend(["", f"The worker result {saved} saved before this stop."])


def _failure(lines: Lines, instruction: Instruction) -> None:
    if instruction.status != "failed":
        return
    if instruction.fix_required is not None:
        _fix_limit(lines, instruction)
        return
    if instruction.operator_reason == "check_proposed":
        _check_proposed(lines, instruction)
        return
    if instruction.dispute is not None:
        _check_disputed(lines, instruction)
        return
    child = _failed_child(instruction)
    if child is None:
        lines.extend(["", *_failed_handler_guidance(instruction)])
        if instruction.recovery_commands and not (
            instruction.workflow_runtime != "single"
            and instruction.caller_role == "worker"
        ):
            _append_section(lines, "Operator recovery")
            lines.extend(
                [
                    "The task is paused here and nothing else will run until the "
                    "user, who is the `ww` operator, decides. Hand over to them "
                    "now, in your own words but covering all of this:",
                    "",
                    "- **What failed**: the command above, and what it printed. "
                    "Quote the output rather than summarising it away.",
                    "- **What is safe**: the work completed so far is saved. "
                    "Nothing is lost by stopping here, and nothing after this "
                    "step has run.",
                    "- **What you need from them**: one of the two choices "
                    "below. Say plainly that you will not pick for them.",
                    "- **What happens next**: if they fix the cause, tell them "
                    "to say so and you will re-run the handler. If they want it "
                    "skipped, tell them you need that in words, and a reason to "
                    "record.",
                    "",
                    "Then wait. Do not retry on a hunch, edit around the "
                    "failure, or move on to another task. When they answer, run "
                    "exactly the option they chose:",
                ]
            )
            for command in instruction.recovery_commands:
                purpose = (
                    "to run the failed handler again, after the cause was fixed"
                    if command.action == "retry"
                    else "to skip it, only with the operator's explicit approval"
                )
                lines.extend(
                    [
                        "",
                        f"{purpose.capitalize()}:",
                        "",
                        "```console",
                        command.command,
                        "```",
                    ]
                )
        return
    _append_section(lines, "Child task recovery")
    lines.extend(
        [
            f"Child `{child.id}` failed. This requires an operator "
            "decision. After addressing the child failure, resume its "
            "workflow with:",
            "",
            "```console",
            next_command(child.task_id),
            "```",
            "",
            "When the child completes, `ww` resumes the parent workflow automatically.",
        ]
    )


def _fix_limit(lines: Lines, instruction: Instruction) -> None:
    """A step whose checks failed as often as they allow: the operator decides."""
    fix = instruction.fix_required
    assert fix is not None
    worker = (
        instruction.workflow_runtime != "single" and instruction.caller_role == "worker"
    )
    lines.extend(
        [
            "",
            f"ww rejected this step's completion {fix.attempt} times; the last "
            "attempt failed these checks:",
        ]
    )
    _fix_failures(lines, fix.failures)
    if worker:
        lines.extend(
            [
                "",
                "Stop here. Return this `ww` response to the manager: the "
                "operator decides how the step continues.",
            ]
        )
        return
    _append_section(lines, "Operator recovery")
    lines.extend(
        [
            "The step is paused and nothing else runs until the user, who is "
            "the `ww` operator, decides. Show them the failures above, quoted, "
            "and ask for one of these choices; do not pick for them.",
        ]
    )
    for command in instruction.recovery_commands:
        purpose = (
            "to give the worker another round of fixes, after the cause is "
            "understood"
            if command.action == "retry"
            else "to complete the step without these checks, only with the "
            "operator's explicit approval; the artifact records the reason"
        )
        lines.extend(
            ["", f"{purpose.capitalize()}:", "", "```console", command.command, "```"]
        )


def _check_disputed(lines: Lines, instruction: Instruction) -> None:
    """The worker disputes a check: its argument, the check, the choices."""
    dispute = instruction.dispute
    assert dispute is not None
    worker = (
        instruction.workflow_runtime != "single" and instruction.caller_role == "worker"
    )
    lines.extend(
        [
            "",
            f"The step's worker disputes `{dispute.failure.id}`, which rejected "
            f"completion attempt {dispute.attempt}. Its argument:",
            "",
            *(f"> {line}" for line in dispute.reason.splitlines()),
        ]
    )
    _fix_failures(lines, (dispute.failure,))
    if worker:
        lines.extend(
            [
                "",
                "Stop here. Return this `ww` response to the manager: the "
                "operator decides whether the check stands.",
            ]
        )
        return
    _append_section(lines, "Operator recovery")
    lines.extend(
        [
            "The step is paused and nothing else runs until the user, who is "
            "the `ww` operator, decides. Show them the check, its output and "
            "the worker's argument above, quoted, and ask for one of these "
            "choices; do not pick for them.",
        ]
    )
    for command in instruction.recovery_commands:
        purpose = (
            "to let the check stand: the worker fixes its work, and the "
            "rejection still counts toward the fix limit"
            if command.action == "retry"
            else f"to waive `{dispute.failure.id}` for this step, only with the "
            "operator's explicit approval; the artifact records the reason"
        )
        lines.extend(
            ["", f"{purpose.capitalize()}:", "", "```console", command.command, "```"]
        )


def _interrupted(lines: Lines, instruction: Instruction) -> None:
    locked_next = f"```console\n{next_command(instruction.task_id)}\n```"
    if instruction.item_status == "interrupted":
        lines.extend(
            [
                "",
                "The external outcome is unknown. Inspect it before choosing "
                "whether to retry or attest success:",
                "",
                locked_next,
                "",
                "Use `next --retry` to replay it, or `next --force "
                "--force-reason` to skip it after operator confirmation.",
            ]
        )
        if instruction.recovery_commands:
            lines.extend(
                [
                    "",
                    "Recovery actions:",
                    "",
                    *(
                        f"- `{recovery.action}`: `{recovery.command}`"
                        for recovery in instruction.recovery_commands
                    ),
                ]
            )
    elif instruction.operation_id and instruction.item_status == "in_progress":
        lines.extend(
            [
                "",
                "This automatic operation is still marked in progress. "
                "Use the locked next command if the process that owns "
                "it is no longer running:",
                "",
                locked_next,
            ]
        )


def _continuation(lines: Lines, instruction: Instruction) -> None:
    if not instruction.continuation_command or instruction.status == "failed":
        return
    if instruction.choosing_outcome_of is not None:
        _outcome_commands(lines, instruction)
        return
    command = instruction.continuation_command
    if instruction.status == "interrupted":
        title = "Recover"
    elif instruction.item_status == "pending" or instruction.manager_input:
        title = "Manager command"
    else:
        title = "Worker completion command"
    _append_section(lines, title)
    if instruction.item_status == "pending":
        worker_caller = (
            instruction.workflow_runtime == "auto"
            and instruction.caller_role == "worker"
        )
        if worker_caller:
            lines.extend(["Return this response to the manager. The manager runs:", ""])
        else:
            lines.extend(["To continue the workflow, run:", ""])
            preview = instruction.assignment_preview
            if (
                instruction.workflow_runtime == "auto"
                and preview
                and "selection_item_name" in preview
            ):
                command = next_command(
                    instruction.task_id,
                    selected_agent=str(preview["requested_agent"]),
                    model=requested_setting(preview["requested_model"]),
                    reasoning=requested_setting(preview["requested_reasoning"]),
                )
                lines.extend(
                    [
                        "These values follow the request. Replace them if "
                        "you select a different available worker.",
                        "",
                    ]
                )
    elif instruction.manager_input:
        lines.extend(["Provide the values and run:", ""])
    elif instruction.next_role == "worker":
        lines.extend(["When the work is finished, run:", ""])
    if instruction.summary_required and instruction.item_status == "in_progress":
        lines.extend(
            [
                "Replace `<one or two sentences for the next step>` with a short "
                "handover the next step will read: what you did and what it must "
                "know. Keep the full detail in the artifact.",
                "",
            ]
        )
    _required_values(lines, instruction)
    _required_metadata(lines, instruction)
    lines.extend(["```console", command, "```"])


def _required_values(lines: Lines, instruction: Instruction) -> None:
    if not instruction.required_values:
        return
    lines.extend(
        [
            "Before ww can continue, replace each `<...>` placeholder "
            "in the command with the value you obtained:",
            "",
            *(
                f"- `{value.name}` — {value.description}"
                for value in instruction.required_values
            ),
        ]
    )
    if any(value.name == "task_id" for value in instruction.required_values):
        lines.extend(
            [
                "",
                "Use the external ID returned by the tracker, such as "
                "`PROJ-482`; do not submit the literal `<task_id>` "
                "placeholder.",
            ]
        )
    if instruction.previous_values:
        lines.extend(
            [
                "",
                "The handler failed with the values it was given last time. "
                "Supply corrected ones, or the same again when the cause lay "
                "elsewhere:",
                "",
                *(
                    f"- `{name}`: {value.replace(chr(10), chr(92) + 'n')}"
                    for name, value in instruction.previous_values
                ),
            ]
        )
    if instruction.automatic_context:
        names = ", ".join(f"`{name}`" for name in instruction.automatic_context)
        lines.extend(
            ["", f"ww will run {names} automatically. Do not run it yourself."]
        )
    lines.append("")


def _required_metadata(lines: Lines, instruction: Instruction) -> None:
    if not instruction.required_metadata:
        return
    scopes = {value.scope for value in instruction.required_metadata}
    scope_label = (
        f"{next(iter(scopes))} metadata"
        if len(scopes) == 1
        else "task and project metadata"
    )
    lines.extend([f"Detect and preserve these {scope_label} values:", ""])
    lines.extend(
        f"- `{value.name}` as `"
        f"{'project_metadata' if value.scope == 'project' else 'metadata'}"
        f".{value.key}`"
        + (f" — {value.description}" if value.description else "")
        + (
            f" (a list: repeat `--metadata {value.name}=<value>` once per value, "
            "or omit it when there is none; earlier values are kept)"
            if value.append
            else ""
        )
        for value in instruction.required_metadata
    )
    lines.append("")


def _manager_intro() -> Lines:
    return [
        "### How `ww` manages this task",
        "",
        "`ww` follows a saved workflow plan, persists progress, runs automatic "
        "handlers, and tells the manager or worker exactly what to do next. "
        "Follow the role and command in each response; there is no need to "
        "inspect `ww-agentic-workflows.yaml`, the `ww` source, or its documentation.",
        "",
    ]


def _failed_handler_guidance(instruction: Instruction) -> Lines:
    if instruction.workflow_runtime != "single" and instruction.caller_role == "worker":
        return [
            "Stop here. Return this `ww` response to the manager for resolution. "
            "Do not fix, rerun, or work around the failed automatic handler.",
        ]
    return [
        "This requires manual intervention from the `ww` operator. Do not retry "
        "on your own, and do not work around the failed automatic handler.",
    ]


def _failed_child(instruction: Instruction) -> ChildTask | None:
    if instruction.workflow_runtime != "single" and instruction.caller_role == "worker":
        return None
    if not instruction.is_child_workflow_control:
        return None
    return next(
        (child for child in instruction.child_tasks if child.status == "failed"), None
    )


def _append_section(lines: Lines, title: str) -> None:
    while lines and lines[-1] == "":
        lines.pop()
    lines.extend(["", f"### {title}", ""])


def _blockquote(value: str) -> Lines:
    return [f"> {line}" if line else ">" for line in value.splitlines()]


def _role(instruction: Instruction) -> str:
    if instruction.loop_limit_reached:
        return "Manager"
    match audience(instruction):
        case Audience.SINGLE_SESSION:
            return "Manager and worker"
        case Audience.MANAGER_DELEGATING | Audience.MANAGER:
            return "Manager"
        case Audience.WORKER_RETURNING | Audience.WORKER:
            return "Worker"


def _outcome_commands(lines: Lines, instruction: Instruction) -> None:
    _append_section(lines, "Choose the outcome")
    lines.extend(
        [
            f"`{instruction.choosing_outcome_of}` is complete. Read its result, "
            "choose the outcome it supports, and run that outcome's command:",
            "",
        ]
    )
    for outcome in instruction.assessment_outcomes:
        lines.extend(
            [
                f"- `{outcome.label}` — {_outcome_effect(outcome)}:",
                "",
                "  ```console",
                "  "
                + next_command(instruction.task_id, outcome=outcome.label),
                "  ```",
            ]
        )


def _action_heading(instruction: Instruction) -> str:
    name = instruction.item_name or "workflow"
    reader = audience(instruction)
    if instruction.choosing_outcome_of is not None:
        # Choosing is the manager's; the worker that assessed hands back.
        if reader is Audience.WORKER_RETURNING:
            return "return control to the manager"
        return f"choose the outcome of `{instruction.choosing_outcome_of}`"
    if instruction.loop_limit_reached:
        return f"escalate the `{name}` loop limit"
    if instruction.is_loop_control:
        return f"advance the `{name}` loop"
    if reader is Audience.MANAGER_DELEGATING:
        verb = "delegate" if instruction.role == "worker" else "perform"
        return f"{verb} the `{instruction.assignment_step or name}` assignment"
    if instruction.item_status == "pending":
        if reader is Audience.WORKER_RETURNING:
            return "return control to the manager"
        return f"dispatch the `{name}` assignment"
    if name == "init":
        return "record the task requirements"
    return f"perform `{name}`"


_ASSIGNMENT_COMPLETE = (
    "This assignment is complete. Stop here: do not run a manager command "
    "or any further `ww` command. Return this `ww` response, a concise "
    "outcome, and artifact references to the manager."
)
_RUN_MANAGER_COMMAND = (
    "You are the manager. Run the displayed manager command yourself."
)


def _assignment_coverage(instruction: Instruction) -> Lines:
    """Name every item one worker performs in this assignment, when several."""
    first, *rest = instruction.assignment_items or ("",)
    if not rest:
        return []
    names = ", ".join(f"`{name}`" for name in (first, *rest))
    performer = (
        "One worker performs" if instruction.role == "worker" else "You perform"
    )
    return [
        f"This assignment covers, in order: {names}. {performer} them "
        "all; `ww` hands each one over after the previous completion.",
        "",
    ]


def _role_instruction(instruction: Instruction) -> Lines:
    reader = audience(instruction)
    if instruction.loop_limit_reached:
        worker_caller = (
            instruction.workflow_runtime == "auto"
            and instruction.caller_role == "worker"
        )
        if worker_caller:
            return [
                "This loop has reached its configured limit. Do not start another "
                "iteration. Return this response and the saved iteration results "
                "to the manager for user escalation.",
                "",
            ]
        return [
            "Do not start another iteration. Report the saved loop results and "
            "this warning to the user for manual resolution.",
            "",
        ]
    if instruction.is_loop_control:
        if reader is Audience.WORKER_RETURNING:
            return [_ASSIGNMENT_COMPLETE, ""]
        return [_RUN_MANAGER_COMMAND, ""]
    if instruction.manager_input:
        return [
            "This assignment only supplies values to an automatic handler. You "
            "are the manager: provide them yourself with the command below. Do "
            "not delegate this to a worker.",
            "",
        ]
    match reader:
        case Audience.SINGLE_SESSION:
            active_role = "worker" if instruction.next_role == "worker" else "manager"
            return [
                "You are both manager and worker in this session. For this "
                f"instruction, act as the {active_role} and do the work yourself. "
                "Do not spawn a subagent.",
                "",
            ]
        case Audience.WORKER_RETURNING:
            return [_ASSIGNMENT_COMPLETE, ""]
        case Audience.MANAGER_DELEGATING if instruction.role == "manager":
            return [
                "You are the manager. This step is yours (`role: manager`): perform "
                "it yourself in this session, not through a worker, and run the "
                "displayed worker completion command.",
                "",
                *_assignment_coverage(instruction),
            ]
        case Audience.MANAGER_DELEGATING:
            return [
                "You are the manager. Select the worker and give it the bootstrap "
                "command below:",
                "",
                *_assignment_coverage(instruction),
            ]
        case Audience.WORKER:
            if instruction.assignment_continues and instruction.completion_registered:
                item = instruction.item_name
                return [
                    f"Same assignment continues: next item `{item}`. "
                    "Do not return to the manager yet. Perform it and run the "
                    "displayed worker completion command.",
                    "",
                ]
            return [
                "You are the worker for this assignment. Perform the work and run "
                "the displayed worker completion command. Continue until `ww` "
                "explicitly returns control to the manager. `ww` saves your "
                "result from that command's `--artifact`; never create or edit "
                "files under `.ww`, except a document this page names.",
                "",
                *_assignment_coverage(instruction),
            ]
        case Audience.MANAGER if instruction.choosing_outcome_of is not None:
            return [_RUN_MANAGER_COMMAND, ""]
        case Audience.MANAGER:
            preview = instruction.assignment_preview
            if preview and "selection_item_name" not in preview:
                # No worker is selected for what comes next; the preview says
                # who performs it.
                return [_RUN_MANAGER_COMMAND, ""]
            return [
                f"{_RUN_MANAGER_COMMAND} Pass "
                "the complete response from `ww next` to the selected worker.",
                "",
            ]


def requested_setting_lines(model: str | None, reasoning: str | None) -> list[str]:
    """Bullet lines for the model and reasoning a step requests, if any."""
    return [
        f"- {label}: `{value}`"
        for label, value in (
            ("Model", requested_setting(model)),
            ("Reasoning", requested_setting(reasoning)),
        )
        if value is not None
    ]
