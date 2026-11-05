# SPDX-License-Identifier: GPL-3.0-or-later
"""``ww migrate``: bring the configuration files, or the stored tasks, up to date.

Without a flag it is a dry run: every deprecated or removed construct the
configuration files hold, with its file and line, and a diff of every file
ww would change. ``--apply`` writes those changes, all files together or
none, then lists what is left. ``--check`` changes nothing and exits 1 while a
removed construct is left; with ``--deprecations``, while any construct is.

A migration with several options changes nothing until the operator
chooses: ``--choose <migration>=<option>``, or at a terminal ``--apply``
asks for each one found. ww never picks an option itself.

The command runs before the configuration is loaded, since a removed
construct is what stops it loading: :func:`migrate_project` finds the files
without loading them when it has to. It reads and edits the checkout's
files, never a configuration snapshot.

``--tasks`` rewrites the stored task state in the current format instead
(:mod:`ww.migrations.tasks`), a dry run without ``--apply``.

Exit codes: 0, also when constructs are left to the operator; 1 for an error,
such as an unknown migration or option in ``--choose``, and for a failed
``--check``.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping

from ww.errors import ConfigurationError, WwError
from ww.extensions import ExtensionRegistry
from ww.migrations.config import ConfigMigration, Finding, write_files
from ww.migrations.config.project import ProjectMigration, migrate_project
from ww.migrations.tasks import TaskRewrites, outdated_tasks, rewrite_tasks
from ww.storage import Storage


def migrate_command(storage: Storage, args: argparse.Namespace) -> int:
    """Run ``ww migrate`` and print its report; the exit code."""
    try:
        _check_flags(args)
        if args.tasks:
            text, code = _tasks(storage, args)
        else:
            text, code = _configuration(storage, args)
    except WwError as error:
        print(f"ww error: {error}", file=sys.stderr)
        return 1
    sys.stdout.write(text)
    return code


def _check_flags(args: argparse.Namespace) -> None:
    if args.deprecations and not args.check:
        raise ConfigurationError("--deprecations goes with --check")
    if args.tasks and (args.check or args.choose or args.deprecations):
        raise ConfigurationError("--tasks takes only --apply and --json")


def _mode(args: argparse.Namespace) -> str:
    return "apply" if args.apply else "check" if args.check else "dry_run"


# ----- configuration -------------------------------------------------------


def _configuration(storage: Storage, args: argparse.Namespace) -> tuple[str, int]:
    extensions = ExtensionRegistry.discover(storage.root, storage.project_config_path)

    def migrate(choices: Mapping[str, str]) -> ProjectMigration:
        return migrate_project(
            storage.config_path, storage.project_config_path, extensions, choices
        )

    result = migrate({})
    choices = _choices(args.choose, result.migrations)
    if args.apply and not args.json_output and _terminal():
        choices.update(_ask(result, choices))
    if choices:
        result = migrate(choices)
    written: tuple[str, ...] = ()
    if args.apply:
        write_files(result.files)
        written = tuple(file.file.label for file in result.changed)
    failing = _failing(result, args.deprecations) if args.check else ()
    code = 1 if failing else 0
    if args.json_output:
        return _json(_configuration_json(result, args, choices, written, failing)), code
    return _configuration_text(result, args, choices, written, failing), code


def _choices(
    values: list[str], migrations: Mapping[str, ConfigMigration]
) -> dict[str, str]:
    """``--choose`` values by migration ID, each a known option of a choice."""
    choices: dict[str, str] = {}
    for value in values:
        migration_id, separator, option = value.partition("=")
        if not separator or not migration_id or not option:
            raise ConfigurationError(f"--choose {value!r} must be <migration>=<option>")
        migration = migrations.get(migration_id)
        if migration is None or migration.kind != "choice":
            known = sorted(
                name for name, item in migrations.items() if item.kind == "choice"
            )
            raise ConfigurationError(
                f"--choose {value!r}: {migration_id!r} is no migration with "
                "options to choose from; "
                + (f"those are: {', '.join(known)}" if known else "none has any")
            )
        names = tuple(item.name for item in migration.options)
        if option not in names:
            raise ConfigurationError(
                f"--choose {value!r}: {migration_id} has no option {option!r}; "
                f"choose one of: {', '.join(names)}"
            )
        choices[migration_id] = option
    return choices


def _terminal() -> bool:
    """Whether the operator is at a terminal to answer a question."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def _pending(
    result: ProjectMigration, choices: Mapping[str, str]
) -> dict[str, list[Finding]]:
    """The places of each choice migration not chosen yet, by its ID."""
    pending: dict[str, list[Finding]] = {}
    for finding in result.findings:
        migration = result.migrations.get(finding.migration)
        if (
            migration is not None
            and migration.kind == "choice"
            and finding.migration not in choices
        ):
            pending.setdefault(finding.migration, []).append(finding)
    return pending


def _ask(result: ProjectMigration, choices: Mapping[str, str]) -> dict[str, str]:
    """Ask the operator which option to apply for each pending choice."""
    chosen: dict[str, str] = {}
    for migration_id, findings in _pending(result, choices).items():
        migration = result.migrations[migration_id]
        print(f"{migration_id}: {migration.why}. Found at:")
        for finding in findings:
            print(f"  {_place(finding)} {finding.location}")
        for number, option in enumerate(migration.options, 1):
            print(f"  {number}. {option.name}: {option.consequence}")
        count = len(migration.options)
        while True:
            try:
                answer = input(
                    f"Apply which option? [1-{count}, Enter to leave it]: "
                ).strip()
            except EOFError:
                answer = ""
            if not answer:
                break
            if answer.isdecimal() and 1 <= int(answer) <= count:
                chosen[migration_id] = migration.options[int(answer) - 1].name
                break
            if answer in (option.name for option in migration.options):
                chosen[migration_id] = answer
                break
            print(f"Answer a number from 1 to {count}, or press Enter.")
        print()
    return chosen


def _failing(result: ProjectMigration, deprecations: bool) -> tuple[Finding, ...]:
    """What fails ``--check``: removed constructs, and with ``--deprecations`` all."""
    return tuple(
        finding
        for finding in result.findings
        if deprecations or finding.kind == "removed"
    )


def _status(
    finding: Finding,
    migration: ConfigMigration | None,
    applied: bool,
) -> str:
    """Where a finding stands: fixed, to choose, or the operator's to change."""
    if finding.fixed:
        return "fixed" if applied else "fixable"
    if migration is not None and migration.kind == "choice":
        return "choose"
    return "manual"


_STATUS_TEXT = {
    "fixed": "fixed",
    "fixable": "ww migrate --apply fixes it",
    "choose": "choose an option",
    "manual": "change it by hand",
}


def _place(finding: Finding) -> str:
    line = finding.line if finding.line is not None else "?"
    return f"{finding.file}:{line}"


def _configuration_text(
    result: ProjectMigration,
    args: argparse.Namespace,
    choices: Mapping[str, str],
    written: tuple[str, ...],
    failing: tuple[Finding, ...],
) -> str:
    applied = bool(args.apply)
    parts: list[str] = []
    if result.unloaded:
        parts.append(
            "The configuration does not load, so its files were found from the "
            "files themselves:\n"
            + "".join(f"  {reason}\n" for reason in result.unloaded)
        )
    findings = result.findings
    if not findings:
        parts.append(
            "Nothing to migrate: no configuration file holds a deprecated or "
            "removed construct.\n"
        )
    else:
        lines = ["Found:\n"]
        for finding in findings:
            migration = result.migrations.get(finding.migration)
            status = _STATUS_TEXT[_status(finding, migration, applied)]
            lines.append(
                f"  {_place(finding)} [{finding.kind}, {status}] "
                f"{finding.migration}\n"
                f"    {finding.location}: {finding.message}\n"
            )
        parts.append("".join(lines))
    changed = result.changed
    if changed and args.apply:
        parts.append("Changed:\n" + "".join(f"  {label}\n" for label in written))
    elif changed and not args.check:
        parts.append(
            "Would change (run ww migrate --apply to write it):\n\n"
            + "\n".join(file.diff() for file in changed)
        )
    pending = _pending(result, choices)
    if pending:
        parts.append(_choices_text(result, pending))
    manual = [
        finding
        for finding in findings
        if _status(finding, result.migrations.get(finding.migration), applied)
        == "manual"
    ]
    if manual:
        parts.append(_manual_text(result, manual))
    if args.check:
        parts.append(_check_text(failing, args.deprecations, len(findings)))
    return "\n".join(parts)


def _choices_text(
    result: ProjectMigration, pending: Mapping[str, list[Finding]]
) -> str:
    lines = ["To choose (ww applies none of the options until you do):\n"]
    for migration_id, findings in pending.items():
        migration = result.migrations[migration_id]
        places = ", ".join(_place(finding) for finding in findings)
        lines.append(f"  {migration_id}: {migration.why} ({places})\n")
        lines.extend(
            f"    - {option.name}: {option.consequence}\n"
            for option in migration.options
        )
        lines.append(
            f"    Apply one with: ww migrate --apply --choose {migration_id}=<option>\n"
            f"    Guide: {migration.reference}\n"
        )
    return "".join(lines)


def _manual_text(result: ProjectMigration, findings: list[Finding]) -> str:
    lines = ["Left for you to change by hand:\n"]
    for finding in findings:
        migration = result.migrations.get(finding.migration)
        lines.append(f"  {_place(finding)} {finding.location}\n    {finding.message}\n")
        if migration is None:
            continue
        if migration.options:
            lines.append("    Options:\n")
            lines.extend(
                f"      - {option.name}: {option.consequence}\n"
                for option in migration.options
            )
        lines.append(f"    Guide: {migration.reference}\n")
    return "".join(lines)


def _check_text(failing: tuple[Finding, ...], deprecations: bool, found: int) -> str:
    if failing:
        what = "construct" if deprecations else "removed construct"
        plural = "s are" if len(failing) != 1 else " is"
        return f"Check failed: {len(failing)} {what}{plural} left.\n"
    if found and not deprecations:
        return (
            "Check passed: no removed construct is left; deprecated ones fail "
            "the check only with --deprecations.\n"
        )
    return "Check passed.\n"


def _configuration_json(
    result: ProjectMigration,
    args: argparse.Namespace,
    choices: Mapping[str, str],
    written: tuple[str, ...],
    failing: tuple[Finding, ...],
) -> dict[str, object]:
    applied = bool(args.apply)
    document: dict[str, object] = {
        "mode": _mode(args),
        "unloaded": list(result.unloaded),
        "findings": [
            _finding_json(finding, result.migrations.get(finding.migration), applied)
            for finding in result.findings
        ],
        "files": [
            {
                "file": file.file.label,
                "path": str(file.file.path),
                "kind": file.file.kind,
                "diff": file.diff(),
            }
            for file in result.changed
        ],
        "choices": [
            {
                "migration": migration_id,
                "why": result.migrations[migration_id].why,
                "options": _options_json(result.migrations[migration_id]),
                "places": [_place(finding) for finding in findings],
                "guide": result.migrations[migration_id].reference,
            }
            for migration_id, findings in _pending(result, choices).items()
        ],
        "chosen": dict(choices),
        "written": list(written),
    }
    if args.check:
        document["check"] = {
            "passed": not failing,
            "deprecations": bool(args.deprecations),
            "failing": len(failing),
        }
    return document


def _finding_json(
    finding: Finding, migration: ConfigMigration | None, applied: bool
) -> dict[str, object]:
    return {
        "file": finding.file,
        "line": finding.line,
        "migration": finding.migration,
        "kind": finding.kind,
        "location": finding.location,
        "message": finding.message,
        "fixed": finding.fixed,
        "status": _status(finding, migration, applied),
        "options": _options_json(migration) if migration is not None else [],
        "guide": migration.reference if migration is not None else None,
    }


def _options_json(migration: ConfigMigration) -> list[dict[str, str]]:
    return [
        {"name": option.name, "consequence": option.consequence}
        for option in migration.options
    ]


# ----- tasks ---------------------------------------------------------------


def _tasks(storage: Storage, args: argparse.Namespace) -> tuple[str, int]:
    rewrites = (
        rewrite_tasks(storage.root) if args.apply else outdated_tasks(storage.root)
    )
    backup = (
        str(rewrites.backup.relative_to(storage.root))
        if rewrites.backup is not None
        else None
    )
    if args.json_output:
        return _json(_tasks_json(rewrites, args, backup)), 0
    return _tasks_text(rewrites, backup), 0


def _tasks_text(rewrites: TaskRewrites, backup: str | None) -> str:
    parts: list[str] = []
    outdated = rewrites.outdated
    listing = "".join(
        f"  {task.task_id}: {', '.join(task.files)}\n" for task in outdated
    )
    if not outdated:
        parts.append("Every stored task that reads is in the current format.\n")
    elif rewrites.applied:
        parts.append(
            f"Rewrote {len(outdated)} task{'s' if len(outdated) != 1 else ''} in the "
            f"current format; their previous state is in {backup}:\n{listing}"
        )
    else:
        several = len(outdated) != 1
        parts.append(
            f"{len(outdated)} task{'s hold' if several else ' holds'} state in "
            "an older format. ww migrate --tasks --apply rewrites "
            f"{'them' if several else 'it'}, after copying "
            f"{'their' if several else 'its'} state to .ww/backups/:\n" + listing
        )
    unreadable = rewrites.unreadable
    if unreadable:
        parts.append(
            "Left as they are, since they do not read:\n"
            + "".join(f"  {task.task_id}: {task.error}\n" for task in unreadable)
        )
    return "\n".join(parts)


def _tasks_json(
    rewrites: TaskRewrites, args: argparse.Namespace, backup: str | None
) -> dict[str, object]:
    return {
        "mode": _mode(args),
        "tasks": [
            {
                "task_id": task.task_id,
                "files": list(task.files),
                "rewritten": rewrites.applied,
            }
            for task in rewrites.outdated
        ],
        "unreadable": [
            {"task_id": task.task_id, "error": task.error}
            for task in rewrites.unreadable
        ],
        "backup": backup,
    }


def _json(value: object) -> str:
    return json.dumps(value, indent=2) + "\n"
