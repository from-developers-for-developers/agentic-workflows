# SPDX-License-Identifier: GPL-3.0-or-later
"""Narrow, redacted audit records for each ww invocation."""

from __future__ import annotations

import argparse
import shlex
from datetime import datetime, timezone

from ww import __version__


def _log_record(
    command: str,
    workflow: str | None,
    task_id: str | None,
    outcome: str,
    error: str | None,
    args: argparse.Namespace,
) -> dict[str, str | None]:
    return {
        "invocation_id": args.invocation_id,
        "timestamp": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "command": command,
        "invocation": _invocation(args),
        "workflow": workflow,
        "task_id": task_id,
        "outcome": outcome,
        # Errors, artifacts, command output, and task state can contain
        # arbitrary user/provider text. The audit log is intentionally narrow
        # and never duplicates that text.
        "error": "protected runtime detail; see task state" if error else None,
        "error_code": "command_failed" if error else None,
        "ww_version": __version__,
        # Who confirmed a gated choice: the operator at a terminal, or an
        # agent carrying out the operator's decision with --yes.
        "confirmation": getattr(args, "confirmation", None),
    }


def _invocation(args: argparse.Namespace) -> str:
    values = ["ww-agentic-workflows", args.command]
    if args.command == "start":
        values.extend(["--workflow", args.workflow])
    task_id = getattr(args, "task_id", None)
    if (
        args.command
        in {
            "start",
            "next",
            "complete",
            "fail",
            "status",
            "instruction",
            "metadata",
            "reset",
        }
        and task_id
    ):
        values.append(task_id)
    if args.command == "start":
        for mode in args.modes:
            values.extend(["--mode", mode])
        values.extend(
            [
                "--agent",
                args.agent,
                "--runtime",
                args.workflow_runtime,
                "--model",
                args.model,
                "--reasoning",
                args.reasoning,
            ]
        )
    if getattr(args, "role", None) is not None:
        values.extend(["--role", args.role])
    if getattr(args, "assignment", None) is not None:
        values.extend(["--assignment", args.assignment])
    if args.command == "plan":
        values.extend(["--workflow", args.workflow, "--agent", args.agent])
        if args.task_id:
            values.extend(["--task-id", args.task_id])
    if args.command == "next":
        values.extend(["--model", args.model, "--reasoning", args.reasoning])
        if args.selected_agent:
            values.extend(["--selected-agent", args.selected_agent])
        if args.force:
            values.append("--force")
        if args.retry:
            values.append("--retry")
        if args.force_reason is not None:
            values.extend(["--reason", "<redacted>"])
        for key in args.approve:
            values.extend(["--approve", key])
        if args.reassign:
            values.append("--reassign")
        if args.yes:
            values.append("--yes")
    if args.command in {"status", "instruction"} and args.run_id:
        values.extend(["--run", args.run_id])
    if args.command == "metadata" and args.project:
        values.append("--project")
    if args.command == "complete":
        for key in ("selected_agent", "selected_model", "selected_reasoning"):
            value = getattr(args, key, None)
            if value is not None:
                values.extend(["--" + key.replace("_", "-"), value])
        for variable in args.variable:
            values.extend(["--variable", _redacted_variable(variable)])
        for metadata_value in args.metadata:
            values.extend(["--metadata", _redacted_variable(metadata_value)])
    if args.command == "fail":
        values.extend(["--error", "<redacted>"])
    if args.command == "reset" and args.yes:
        values.append("--yes")
    if getattr(args, "json_output", False):
        values.append("--json")
    return shlex.join(values)


def _redacted_variable(value: str) -> str:
    name, separator, _ = value.partition("=")
    return f"{name}=<redacted>" if separator else "<redacted>"
