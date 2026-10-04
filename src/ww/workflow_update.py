# SPDX-License-Identifier: GPL-3.0-or-later
"""``ww setup update``: replace one workflow definition where it is written.

``setup apply`` adds definitions to ww's own setup files. Changing a workflow
the project already defines, in its root YAML or in any imported file, needs
more care: the edit must land in the definition that is in force, and nothing
else in the file may move. The update path is deliberately small:

- the fragment holds ``workflows`` with exactly one entry, named like the
  workflow being updated, and nothing else;
- the target is the definition that wins in the composed configuration; with
  ``--level``, the one at that level, refused when a higher-precedence
  definition hides it, since the edit would report success and change nothing;
- only that list item's lines are replaced, in ww's YAML style; every other
  line of the file, comments and other definitions included, stays byte for
  byte. Comments inside the replaced entry are the unavoidable loss, and the
  preview says so;
- the result is checked to load back as the old data with exactly that entry
  swapped, then validated in memory as ww would load it, and the workflow's
  provenance must point at the file written;
- a workflow that only ww ships, or one that is not defined anywhere, is
  refused with the command to use instead.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ww.config import load_configuration
from ww.config.composition import WorkflowSite, entry_name, workflow_sites
from ww.config_files import read_configuration_file, staged_files
from ww.config_writes import FileWrite, Transaction, dump_yaml
from ww.errors import StateError, WwError
from ww.extensions import ExtensionRegistry
from ww.workflow_config import WorkflowConfigLevel

LEVELS = ("local", "project", "global")


@dataclass(frozen=True)
class UpdatePlan:
    """The one file an update writes, and what the operator is shown first."""

    name: str
    level: WorkflowConfigLevel
    label: str
    write: FileWrite
    diff: str
    notes: tuple[str, ...]


def plan_update(
    root: Path, config_path: Path, name: str, fragment: Path, level: str | None
) -> UpdatePlan:
    """Plan replacing workflow ``name``; nothing is written."""
    if level is not None and level not in LEVELS:
        raise StateError(f"--level takes {', '.join(LEVELS)}")
    entry = _read_fragment(fragment, name)
    site = _target(config_path, name, level)
    text = read_configuration_file(site.file)
    new_text, comments = _replace_entry(text, name, entry, site.label)
    diff = "".join(
        difflib.unified_diff(
            text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            f"a/{site.label}",
            f"b/{site.label}",
        )
    )
    notes = ["the entry is rewritten in ww's YAML style; the rest of the file is kept"]
    if comments:
        notes.append("comments inside the replaced entry are not kept")
    return UpdatePlan(
        name, site.level, site.label, FileWrite(site.file, new_text), diff, tuple(notes)
    )


def validate_update(root: Path, config_path: Path, plan: UpdatePlan) -> None:
    """Load the configuration as the plan leaves it; nothing is written."""
    with staged_files({plan.write.path: plan.write.content}):
        try:
            configuration = load_configuration(
                config_path, ExtensionRegistry.discover(root)
            )
        except WwError as error:
            raise StateError(
                f"refused: the configuration would not be valid ({error}); "
                "nothing was written"
            ) from error
    provenance = configuration.workflow_provenance.get(plan.name)
    if provenance is None or provenance.source != plan.label:
        winner = provenance.source if provenance else "another definition"
        raise StateError(
            f"refused: after this edit workflow `{plan.name}` would still come "
            f"from {winner}, not {plan.label}; nothing was written"
        )


def apply_update(plan: UpdatePlan) -> None:
    with Transaction() as transaction:
        transaction.apply([plan.write])


def render_plan(plan: UpdatePlan) -> str:
    lines = [
        f"`ww setup update` replaces workflow `{plan.name}` in {plan.label} "
        f"({plan.level} level):",
        *(f"Note: {note}" for note in plan.notes),
        plan.diff.rstrip("\n"),
    ]
    return "\n".join(lines) + "\n"


def plan_to_dict(plan: UpdatePlan, *, applied: bool) -> dict[str, object]:
    return {
        "workflow": plan.name,
        "file": plan.label,
        "level": plan.level,
        "notes": list(plan.notes),
        "diff": plan.diff,
        "applied": applied,
    }


def _read_fragment(path: Path, name: str) -> Any:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise StateError(f"cannot read {path}: {error}") from error
    except yaml.YAMLError as error:
        raise StateError(f"invalid YAML in {path}: {error}") from error
    entries = raw.get("workflows") if isinstance(raw, dict) else None
    if not isinstance(raw, dict) or set(raw) != {"workflows"}:
        raise StateError(
            f"{path} must hold only `workflows` with the one workflow to "
            "update; other changes go through `ww setup apply`"
        )
    if not isinstance(entries, list) or len(entries) != 1:
        raise StateError(f"{path}: workflows must list exactly one workflow")
    if entry_name(entries[0]) != name:
        raise StateError(
            f"{path} defines `{entry_name(entries[0])}`, not `{name}`; an update "
            "does not rename"
        )
    return entries[0]


def _target(config_path: Path, name: str, level: str | None) -> WorkflowSite:
    sites = workflow_sites(config_path, name)
    if not sites:
        raise StateError(
            f"no configuration file defines workflow `{name}`; add or override "
            "one with `ww setup apply`"
        )
    winner = sites[-1]
    if level is None:
        return winner
    chosen = [site for site in sites if site.level == level]
    if not chosen:
        where = ", ".join(f"{site.label} ({site.level})" for site in sites)
        raise StateError(
            f"workflow `{name}` has no definition at the {level} level; it is "
            f"defined in {where}"
        )
    if chosen[-1] is not winner:
        raise StateError(
            f"refused: workflow `{name}` at the {level} level ({chosen[-1].label}) "
            f"is hidden by {winner.label} ({winner.level} level), which takes "
            "precedence, so editing it would change nothing; update that "
            f"definition (--level {winner.level}) or remove it first"
        )
    return chosen[-1]


def _replace_entry(text: str, name: str, entry: Any, label: str) -> tuple[str, bool]:
    """``text`` with workflow ``name``'s list item replaced, and nothing else."""
    try:
        raw = yaml.safe_load(text)
        document = yaml.compose(text)
    except yaml.YAMLError as error:
        raise StateError(f"invalid YAML in {label}: {error}") from error
    workflows = raw["workflows"]
    index = next(i for i, item in enumerate(workflows) if entry_name(item) == name)
    if workflows[index] == entry:
        raise StateError(f"workflow `{name}` in {label} already is that")
    node = next(
        value for key, value in document.value if key.value == "workflows"
    ).value[index]
    lines = text.splitlines(keepends=True)
    first = node.start_mark.line
    prefix = lines[first][: node.start_mark.column].rstrip()
    if not prefix.endswith("-"):
        raise _cannot(label)
    dash = len(prefix) - 1
    end = node.end_mark
    if end.line >= len(lines):
        after = len(lines)
    elif lines[end.line][: end.column].strip() == "":
        after = end.line
    else:
        after = end.line + 1
    while after - 1 > first and (
        not lines[after - 1].strip() or lines[after - 1].lstrip().startswith("#")
    ):
        after -= 1
    old = lines[first:after]
    comments = any(line.lstrip().startswith("#") or " #" in line for line in old)
    shift = dash - 2
    block = dump_yaml({"workflows": [entry]}).splitlines(keepends=True)[1:]
    if shift > 0:
        block = [" " * shift + line if line.strip() else line for line in block]
    elif shift < 0:
        block = [line[-shift:] if line.strip() else line for line in block]
    if old and not old[-1].endswith("\n"):
        block[-1] = block[-1].rstrip("\n")
    updated = "".join([*lines[:first], *block, *lines[after:]])
    expected = {
        **raw,
        "workflows": [*workflows[:index], entry, *workflows[index + 1 :]],
    }
    try:
        reloaded = yaml.safe_load(updated)
    except yaml.YAMLError:
        raise _cannot(label) from None
    if reloaded != expected:
        raise _cannot(label)
    return updated, comments


def _cannot(label: str) -> StateError:
    return StateError(
        f"ww cannot replace that workflow in {label} without changing anything "
        "else; edit it by hand"
    )
