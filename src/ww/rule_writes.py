# SPDX-License-Identifier: GPL-3.0-or-later
"""Validated writes of rule files and rule groups for ``ww rules add`` and kin.

The ``ww-rule`` skill carries the judgment of turning an operator's words into
rules; these commands carry out its decisions on disk, and nothing else writes
rules for it. Each command is planned first (:class:`RuleWrite`: the files to
write or delete and the directories to create), then applied together and
validated by loading the configuration as ww would. When the configuration
would not load, or the write would not have the effect planned, every file is
put back as it was and the command fails: a write never leaves the project in
a state ``ww lint`` rejects. Nothing is committed; rule files are
configuration, committed with the change that needs them.

Rule files are edited in place. The repo's ``ww-agentic-workflows.yaml`` is
never rewritten: a new root group goes into ``ww-rules.yaml``, a file ww owns
and rewrites whole, which the repo file lists under ``imports``; adding that
one list entry is the only change ww makes to the repo file, and it is checked
to leave every other value as it was. A group declared anywhere else is the
operator's own, so ``rules filter`` refuses it and says what to write.

``rules promote`` is the one write that also changes the rule-automation
store: it copies an approved check's command into the ``check`` of every rule
file the check covers, then deletes the check and those rules' entries, since
a rule with its own command is never looked up in the store again.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import yaml

from ww.changes import project_files, select_files
from ww.config import load_configuration
from ww.config.composition import ComposedConfiguration
from ww.config.rules import (
    parse_check_command,
    rule_summary,
    rule_text_hash,
)
from ww.config_files import RULES_IMPORT_FILE, display_path
from ww.errors import ConfigurationError, StateError
from ww.extensions import ExtensionRegistry
from ww.rule_store import RuleAutomation, RuleStore, describe_command
from ww.workflow_config import (
    INIT_STEP_NAME,
    ItemFlow,
    RuleDefinition,
    RuleGroup,
    RuleGroupRef,
    StepDefinition,
    WorkflowConfiguration,
    every_step,
)

# A rule file's stem: lower-case kebab-case, as the derived ones are.
STEM = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
STEM_WORDS = 5
_WORD = re.compile(r"[a-z0-9]+")
_DELIMITER = "---\n"
_IMPORT_HEADER = (
    "# Rule groups written by `ww rules add --group` and `ww rules filter`.\n"
    "# ww rewrites this file whole; the repo file imports it.\n"
)


@dataclass(frozen=True)
class FileWrite:
    """One file's new content, or ``None`` to delete it."""

    path: Path
    content: str | None


@dataclass(frozen=True)
class RuleWrite:
    """What one command changes on disk, and what it tells the operator.

    ``rule`` or ``group`` names what must exist in the configuration once
    the write is applied, and whose placement the report shows. ``promoted``
    is the store check ``rules promote`` deletes, with the rule hashes whose
    entries go with it.
    """

    writes: tuple[FileWrite, ...]
    report: tuple[str, ...]
    directories: tuple[Path, ...] = ()
    warnings: tuple[str, ...] = ()
    rule: str | None = None
    group: str | None = None
    promoted: tuple[str, tuple[str, ...]] | None = None


@dataclass(frozen=True)
class RuleProject:
    """The project a write applies to, with its configuration as it is now.

    ``configuration`` is the validated configuration before the write, and
    ``composed`` the composed YAML it was parsed from.
    """

    root: Path
    config_path: Path
    configuration: WorkflowConfiguration
    composed: ComposedConfiguration
    extensions: ExtensionRegistry | None = None

    @property
    def base(self) -> Path:
        """The directory rule paths in the composed configuration resolve against."""
        return self.config_path.parent

    def label(self, path: Path) -> str:
        return display_path(path, self.root)

    def group(self, name: str) -> RuleGroup:
        group = self.configuration.rule_groups_by_name.get(name)
        if group is None:
            known = ", ".join(sorted(self.configuration.rule_groups_by_name))
            raise StateError(
                f"no rule group {name!r}"
                + (f"; the groups are {known}" if known else "; there are no groups")
            )
        return group


# Planning ---------------------------------------------------------------------


def rule_stem(text: str) -> str:
    """A file stem from a rule's first sentence: at most five kebab-case words."""
    words = _WORD.findall(rule_summary(text).lower())[:STEM_WORDS]
    if not words:
        raise StateError("the rule's first sentence has no words to name it; pass --id")
    return "-".join(words)


def check_mapping(
    shell: str | None, argv: tuple[str, ...] | None, assertion: str | None
) -> dict[str, Any] | None:
    """A rule file's ``check`` from the command-line options, validated."""
    if shell is None and not argv:
        if assertion is not None:
            raise StateError("--assert needs --check-shell or --check-argv")
        return None
    mapping: dict[str, Any] = (
        {"shell": shell} if shell is not None else {"argv": list(argv or ())}
    )
    if assertion is not None:
        mapping["assert"] = _assertion(assertion)
    try:
        parse_check_command(dict(mapping), "--check")
    except ConfigurationError as error:
        raise StateError(str(error)) from error
    return mapping


def _assertion(value: str) -> dict[str, str]:
    if value == "empty":
        return {"operator": "empty"}
    operator, separator, expected = value.partition(":")
    if operator == "eq" and separator:
        return {"operator": "eq", "expected": expected}
    raise StateError(f"--assert takes empty or eq:<value>, not {value!r}")


def plan_add_rule(
    project: RuleProject,
    group_name: str,
    text: str,
    *,
    paths: tuple[str, ...] = (),
    check: dict[str, Any] | None = None,
    stem: str | None = None,
) -> RuleWrite:
    """A new rule file in the group's first directory, never over another."""
    body = _body(text)
    group = project.group(group_name)
    directory = _group_directory(project, group)
    if stem is not None and not STEM.fullmatch(stem):
        raise StateError(f"--id {stem!r} must be lower-case kebab-case")
    stem = stem or rule_stem(body)
    file = directory / f"{stem}.md"
    if file.exists():
        raise StateError(
            f"{project.label(file)} already exists; choose another --id, or "
            f"amend that rule with `rules edit {group.name}/{stem}`"
        )
    frontmatter: dict[str, Any] = {}
    if paths:
        frontmatter["paths"] = list(paths)
    if check is not None:
        frontmatter["check"] = check
    content = (
        f"---\n{_dump(frontmatter)}---\n{body}" if frontmatter else body
    )
    rule_id = f"{group.name}/{stem}"
    return RuleWrite(
        writes=(FileWrite(file, content),),
        report=(
            f"Created {project.label(file)}: rule `{rule_id}`.",
            *_glob_report(project, paths),
        ),
        warnings=_glob_warnings(project, paths),
        rule=rule_id,
    )


def plan_add_group(
    project: RuleProject,
    name: str,
    directory: Path,
    *,
    workflows: tuple[str, ...] | None = None,
    steps: tuple[str, ...] | None = None,
) -> RuleWrite:
    """A new root group in ``ww-rules.yaml``, imported by the repo file."""
    if name in project.configuration.rule_groups_by_name:
        raise StateError(f"rule group {name!r} already exists")
    folder = _inside(project, directory)
    if folder.exists() and not folder.is_dir():
        raise StateError(f"{project.label(folder)} is a file, not a directory")
    imports = _RulesImport.read(project)
    groups = dict(imports.groups)
    if name in groups:  # pragma: no cover - the configuration would have it
        raise StateError(f"rule group {name!r} already exists in {imports.label}")
    item = Path(os.path.relpath(folder, imports.path.parent)).as_posix() + "/"
    groups[name] = _group_entry([item], workflows, steps)
    report = [f"Added rule group `{name}` ({item}) to {imports.label}."]
    writes = [imports.write(groups)]
    if not imports.imported:
        writes.append(imports.import_it(project))
        report.append(
            f"Added {imports.label} to imports in {project.label(project.config_path)}."
        )
    warnings: tuple[str, ...] = ()
    if not folder.is_dir() or not any(folder.glob("*.md")):
        warnings = (
            f"{project.label(folder)} holds no rule yet, and git does not keep an "
            f"empty directory: add one with `rules add {name} --text ...` before "
            "committing.",
        )
    return RuleWrite(
        writes=tuple(writes),
        report=tuple(report),
        directories=(folder,) if not folder.is_dir() else (),
        warnings=warnings,
        group=name,
    )


def plan_edit(
    project: RuleProject,
    automation: RuleAutomation,
    rule_id: str,
    *,
    text: str | None = None,
    paths: tuple[str, ...] | None = None,
) -> RuleWrite:
    """A rule file with a new body, new globs, or both; the rest kept as it is."""
    if text is None and paths is None:
        raise StateError("rules edit needs --text, --paths, or both")
    rule, file = _rule_file(project, rule_id)
    opening, frontmatter, closing, body = _split(file.read_text(encoding="utf-8"))
    report = [f"Edited {project.label(file)}: rule `{rule_id}`."]
    warnings: list[str] = []
    if paths is not None:
        frontmatter = _set_key(frontmatter or "", "paths", list(paths), file)
        opening, closing = opening or _DELIMITER, closing or _DELIMITER
        report.extend(_glob_report(project, paths))
        warnings.extend(_glob_warnings(project, paths))
    if text is not None:
        body = _body(text)
        if rule_text_hash(body) == rule.text_hash:
            report.append("The wording is unchanged apart from whitespace.")
        warnings.extend(_wording_warnings(automation, rule, body))
    content = (
        f"{opening}{frontmatter}{closing}{body}" if opening is not None else body
    )
    return RuleWrite(
        writes=(FileWrite(file, content),),
        report=tuple(report),
        warnings=tuple(warnings),
        rule=rule_id,
    )


def plan_move(project: RuleProject, rule_id: str, group_name: str) -> RuleWrite:
    """The rule's file moved, unchanged, into another group's first directory."""
    _, file = _rule_file(project, rule_id)
    group = project.group(group_name)
    target = _group_directory(project, group) / file.name
    if target.resolve() == file.resolve():
        raise StateError(f"{project.label(file)} is already in group {group.name!r}")
    if target.exists():
        raise StateError(
            f"{project.label(target)} already exists; the group has a rule named "
            f"{file.stem!r}"
        )
    new_id = f"{group.name}/{file.stem}"
    return RuleWrite(
        writes=(
            FileWrite(target, file.read_text(encoding="utf-8")),
            FileWrite(file, None),
        ),
        report=(
            f"Moved {project.label(file)} to {project.label(target)}: rule "
            f"`{rule_id}` is now `{new_id}`. Its wording is unchanged, so what the "
            "rule-automation store knows about it still applies.",
        ),
        rule=new_id,
    )


def plan_filter(
    project: RuleProject,
    group_name: str,
    *,
    workflows: tuple[str, ...] | None,
    steps: tuple[str, ...] | None,
    all_workflows: bool = False,
    all_steps: bool = False,
) -> RuleWrite:
    """New ``workflows``/``steps`` filters for a group in ``ww-rules.yaml``.

    ``None`` leaves a filter as it is; an empty tuple admits nothing, so the
    group applies only where a step names it; ``all_*`` removes the filter.
    """
    if workflows is None and steps is None and not all_workflows and not all_steps:
        raise StateError(
            "rules filter needs --workflows, --steps, --all-workflows, or --all-steps"
        )
    if (workflows is not None and all_workflows) or (steps is not None and all_steps):
        raise StateError("--all-workflows and --all-steps replace a filter; pass one")
    group = project.group(group_name)
    imports = _RulesImport.read(project)
    if group_name not in imports.groups or not imports.imported:
        raise StateError(
            f"rule group {group_name!r} is declared in "
            f"{_declared_in(project, group)}, which ww does not rewrite; set its "
            f"filters there by hand ({_filter_yaml(group_name, workflows, steps)})"
        )
    winner = next(
        (
            override.overridden_by
            for override in project.composed.overrides
            if override.kind == "rule group"
            and override.name == group_name
            and override.overridden_in == imports.label
        ),
        None,
    )
    if winner is not None:
        raise StateError(
            f"rule group {group_name!r} is declared again in {winner}, which "
            f"replaces the one in {imports.label}; change it there"
        )
    entry = dict(_group_mapping(imports.groups[group_name]))
    for key, value, clear in (
        ("workflows", workflows, all_workflows),
        ("steps", steps, all_steps),
    ):
        if clear:
            entry.pop(key, None)
        elif value is not None:
            entry[key] = list(value)
    groups = {**imports.groups, group_name: entry}
    return RuleWrite(
        writes=(imports.write(groups),),
        report=(
            f"Changed the filters of rule group `{group_name}` in {imports.label}.",
        ),
        group=group_name,
    )


def plan_promote(
    project: RuleProject, automation: RuleAutomation, name: str
) -> RuleWrite:
    """An approved store check copied into the rule files it covers."""
    check = automation.checks.get(name)
    if check is None:
        known = ", ".join(sorted(automation.checks))
        raise StateError(
            f"the rule-automation store has no check {name!r}"
            + (f"; its checks are {known}" if known else "")
        )
    if check.status != "converted" or check.pending is not None:
        raise StateError(
            f"check {name!r} is {check.status}"
            + (" with a revision awaiting approval" if check.pending else "")
            + "; only an approved check without a pending revision is promoted"
        )
    waiting = sorted(
        key[:12]
        for key, entry in automation.rules.items()
        if entry.check == name and entry.status != "converted"
    )
    if waiting:
        raise StateError(
            f"rules {', '.join(waiting)} wait on a decision about check {name!r}; "
            "decide them first"
        )
    covers = check.spec.covers
    declared = _declared_rules(project.configuration)
    rules = [rule for key in covers for rule in declared.get(key, ())]
    if not rules:
        raise StateError(
            f"no declared rule has the wording check {name!r} covers; "
            "`rules prune` removes it"
        )
    inline = sorted({rule.id for rule in rules if rule.source is None})
    if inline:
        raise StateError(
            f"check {name!r} covers {', '.join(inline)}, written in a step's own "
            "`rules` list rather than a rule file; give that rule its command "
            "there by hand"
        )
    command = dict(check.spec.command.commands[0].to_dict())
    if check.spec.command.assertion is not None:
        command["assert"] = check.spec.command.assertion.to_dict()
    writes: dict[Path, FileWrite] = {}
    for rule in rules:
        if rule.check is not None:
            raise StateError(f"rule `{rule.id}` already has a check of its own")
        file = _inside(project, Path(str(rule.source)))
        if file in writes:
            continue
        opening, frontmatter, closing, body = _split(file.read_text(encoding="utf-8"))
        frontmatter = _set_key(frontmatter or "", "check", command, file)
        writes[file] = FileWrite(
            file, f"{opening or _DELIMITER}{frontmatter}{closing or _DELIMITER}{body}"
        )
    report = [
        f"Promoted check `{name}` ({describe_command(check.spec.command)}) into "
        + ", ".join(project.label(file) for file in writes)
        + "; removed it and its rules' entries from the rule-automation store.",
    ]
    warnings = []
    if len(writes) > 1:
        warnings.append(
            f"The command now runs once for each of these {len(writes)} rules, "
            "on the changed files each rule's globs select, instead of once."
        )
    if check.spec.config:
        report.append(
            "Its configuration stays where it is: " + ", ".join(check.spec.config)
        )
    return RuleWrite(
        writes=tuple(writes.values()),
        report=tuple(report),
        warnings=tuple(warnings),
        rule=rules[0].id,
        promoted=(name, covers),
    )


# Applying ---------------------------------------------------------------------


@dataclass(frozen=True)
class WriteOutcome:
    """What a write did, or would do under ``--dry-run``."""

    report: tuple[str, ...]
    warnings: tuple[str, ...]
    placement: tuple[str, ...]
    applied: bool

    def render(self) -> str:
        lines = list(self.report)
        lines.extend(f"Warning: {warning}" for warning in self.warnings)
        if self.placement:
            lines.append("Reaches these steps (an agent step's page shows it):")
            lines.extend(f"- {line}" for line in self.placement)
        else:
            lines.append(
                "Reaches no step yet: no filter admits one and no step names it."
            )
        if not self.applied:
            lines.append(
                "Dry run: the configuration would be valid; nothing was written."
            )
        return "\n".join(lines) + "\n"


def apply_write(
    project: RuleProject, write: RuleWrite, *, dry_run: bool = False
) -> WriteOutcome:
    """Write, validate by loading the configuration, and keep it or put it back.

    A dry run always puts the files back, after the same validation.
    """
    with _Transaction() as transaction:
        transaction.apply(write)
        try:
            configuration = load_configuration(project.config_path, project.extensions)
            _expect(configuration, write)
        except ConfigurationError as error:
            transaction.roll_back()
            raise StateError(
                f"refused: the configuration would not be valid ({error}); "
                "nothing was written"
            ) from error
        placement = _placement(configuration, write)
        if dry_run:
            transaction.roll_back()
    if write.promoted is not None and not dry_run:
        name, covers = write.promoted
        RuleStore(project.root).modify(
            lambda current: _without_check(current, name, covers)
        )
    return WriteOutcome(write.report, write.warnings, placement, not dry_run)


def _expect(configuration: WorkflowConfiguration, write: RuleWrite) -> None:
    if write.group is not None and write.group not in configuration.rule_groups_by_name:
        raise ConfigurationError(f"rule group {write.group!r} is not declared")
    if write.rule is not None and write.rule not in _declared_rule_ids(configuration):
        raise ConfigurationError(f"no rule {write.rule!r} is declared")


def _without_check(
    automation: RuleAutomation, name: str, covers: tuple[str, ...]
) -> RuleAutomation:
    return replace(
        automation,
        checks={key: entry for key, entry in automation.checks.items() if key != name},
        rules={
            key: entry
            for key, entry in automation.rules.items()
            if not (key in covers and entry.check == name)
        },
    )


class _Transaction:
    """Files written together and restored together when anything fails."""

    def __init__(self) -> None:
        self._saved: list[tuple[Path, bytes | None]] = []
        self._created: list[Path] = []
        self._done = False

    def __enter__(self) -> _Transaction:
        return self

    def __exit__(self, kind: object, error: object, trace: object) -> None:
        if error is not None:
            self.roll_back()

    def apply(self, write: RuleWrite) -> None:
        for directory in write.directories:
            self._make_directory(directory)
        for change in write.writes:
            self._saved.append(
                (
                    change.path,
                    change.path.read_bytes() if change.path.exists() else None,
                )
            )
            if change.content is None:
                change.path.unlink()
            else:
                self._make_directory(change.path.parent)
                _atomic_write(change.path, change.content)

    def _make_directory(self, directory: Path) -> None:
        missing = [
            folder for folder in (directory, *directory.parents) if not folder.exists()
        ]
        for folder in reversed(missing):
            folder.mkdir()
            self._created.append(folder)

    def roll_back(self) -> None:
        if self._done:
            return
        self._done = True
        for path, content in reversed(self._saved):
            if content is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(content)
        for folder in reversed(self._created):
            if folder.is_dir() and not any(folder.iterdir()):
                folder.rmdir()


def _atomic_write(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.ww-tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


# Where a rule applies ---------------------------------------------------------


def _walk(
    steps: tuple[StepDefinition, ...], parent: str | None = None
) -> Iterator[tuple[StepDefinition, str]]:
    """Every step with its logical path, as the plan compiler names them."""
    for step in steps:
        path = f"{parent}/{step.name}" if parent else step.name
        yield step, path
        nested = (
            *step.child_steps,
            *step.loop_steps,
            *(step.items.steps if isinstance(step.items, ItemFlow) else ()),
        )
        yield from _walk(nested, path)


def _placement(
    configuration: WorkflowConfiguration, write: RuleWrite
) -> tuple[str, ...]:
    """``workflow: step, step`` for each workflow the rule or group reaches."""
    groups = configuration.rule_groups_by_name

    def reaches(group: RuleGroup) -> bool:
        return write.group == group.name or any(
            rule.id == write.rule for rule in group.rules
        )

    lines = []
    for workflow in configuration.workflows:
        walked = tuple(_walk(workflow.steps))
        precise = frozenset(path for _, path in walked)
        names = []
        for step, path in walked:
            if step.name == INIT_STEP_NAME:
                continue
            admitted = any(
                reaches(group)
                and group.applies_to(workflow.name, step.name, path, precise)
                for group in configuration.rule_groups
            )
            named = any(
                (isinstance(entry, RuleGroupRef) and reaches(groups[entry.name]))
                or (isinstance(entry, RuleDefinition) and entry.id == write.rule)
                for entry in step.rules
            )
            if admitted or named:
                names.append(path)
        if names:
            lines.append(f"{workflow.name}: " + ", ".join(dict.fromkeys(names)))
    return tuple(lines)


# Helpers ----------------------------------------------------------------------


def _body(text: str) -> str:
    body = text.strip()
    if not body:
        raise StateError("--text must hold the rule's sentence")
    if body.splitlines()[0].strip() == "---":
        raise StateError(
            "--text must not start with a --- line, which opens frontmatter"
        )
    return body + "\n"


def _dump(value: dict[str, Any]) -> str:
    return yaml.safe_dump(  # type: ignore[no-any-return]
        value, sort_keys=False, allow_unicode=True, default_flow_style=None, width=1000
    )


def _inside(project: RuleProject, path: Path) -> Path:
    """``path`` resolved, relative to the root, refused outside the project."""
    resolved = (path if path.is_absolute() else project.root / path).resolve()
    try:
        resolved.relative_to(project.root.resolve())
    except ValueError:
        raise StateError(
            f"{path} is outside the project; ww writes rules only inside it"
        ) from None
    return resolved


def _group_directory(project: RuleProject, group: RuleGroup) -> Path:
    """The first directory a group lists, where its new rules go."""
    if group.origin != "configuration":
        origin = group.origin.removeprefix("extension ")
        raise StateError(
            f"rule group {group.name!r} is shipped by {origin}; "
            "add the rule to a group of the project's configuration"
        )
    declared = project.composed.raw.get("rules")
    raw = declared.get(group.name) if isinstance(declared, dict) else None
    items = _group_mapping(raw).get("rules", []) if raw is not None else []
    names = project.configuration.rule_groups_by_name
    for item in items:
        if isinstance(item, str) and item not in names:
            candidate = Path(item) if Path(item).is_absolute() else project.base / item
            if candidate.is_dir():
                return _inside(project, candidate)
    raise StateError(
        f"rule group {group.name!r} lists no directory to add a rule file to; "
        f"add one to it, or use a group that has one"
    )


def _group_mapping(raw: Any) -> dict[str, Any]:
    if isinstance(raw, list):
        return {"rules": list(raw)}
    if isinstance(raw, dict):
        return dict(raw)
    raise StateError(f"rule group entry {raw!r} is neither a list nor a mapping")


def _group_entry(
    items: list[str],
    workflows: tuple[str, ...] | None,
    steps: tuple[str, ...] | None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {"rules": items}
    if workflows is not None:
        entry["workflows"] = list(workflows)
    if steps is not None:
        entry["steps"] = list(steps)
    return entry


def _filter_yaml(
    name: str, workflows: tuple[str, ...] | None, steps: tuple[str, ...] | None
) -> str:
    parts = [f"{key}: [{', '.join(value)}]" for key, value in (
        ("workflows", workflows), ("steps", steps)) if value is not None]
    return f"rules.{name}: " + ("; ".join(parts) if parts else "remove the filter keys")


def _declared_in(project: RuleProject, group: RuleGroup) -> str:
    if group.origin != "configuration":
        return group.origin
    origin = next(
        (
            override.overridden_by
            for override in reversed(project.composed.overrides)
            if override.kind == "rule group" and override.name == group.name
        ),
        None,
    )
    if origin is not None:
        return origin
    for label in reversed(project.composed.sources):
        path = project.base / label
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if isinstance(raw, dict) and isinstance(raw.get("rules"), dict) and (
            group.name in raw["rules"]
        ):
            return label
    return project.label(project.config_path)


def _declared_rules(
    configuration: WorkflowConfiguration,
) -> dict[str, tuple[RuleDefinition, ...]]:
    """Every declared rule by wording hash, each rule ID once."""
    found: dict[str, dict[str, RuleDefinition]] = {}
    for rule in _every_rule(configuration):
        found.setdefault(rule.text_hash, {}).setdefault(rule.id, rule)
    return {key: tuple(rules.values()) for key, rules in found.items()}


def _declared_rule_ids(configuration: WorkflowConfiguration) -> set[str]:
    return {rule.id for rule in _every_rule(configuration)}


def _every_rule(configuration: WorkflowConfiguration) -> Iterator[RuleDefinition]:
    for group in configuration.rule_groups:
        yield from group.rules
    for step in every_step(configuration):
        for entry in step.rules:
            if isinstance(entry, RuleDefinition):
                yield entry


def _rule_file(project: RuleProject, rule_id: str) -> tuple[RuleDefinition, Path]:
    """A declared rule and the file it lives in, which ww may change."""
    rule = next(
        (rule for rule in _every_rule(project.configuration) if rule.id == rule_id),
        None,
    )
    if rule is None:
        raise StateError(
            f"no rule {rule_id!r} is declared; `rules` lists every rule ID"
        )
    group = project.configuration.rule_groups_by_name.get(rule_id.split("/", 1)[0])
    if group is not None and group.origin != "configuration" and any(
        candidate.id == rule_id for candidate in group.rules
    ):
        raise StateError(
            f"rule `{rule_id}` is shipped by {group.origin}; "
            "ww does not change it"
        )
    if rule.source is None:
        raise StateError(
            f"rule `{rule_id}` is written in a step's own `rules` list in the "
            "YAML, which ww does not rewrite; change it there"
        )
    return rule, _inside(project, Path(rule.source))


def _split(content: str) -> tuple[str | None, str | None, str | None, str]:
    """A rule file's opening ``---``, frontmatter, closing ``---``, and body.

    Without frontmatter the first three are ``None``. The parts join back to
    the file byte for byte.
    """
    lines = content.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return None, None, None, content
    for index, line in enumerate(lines[1:], 1):
        if line.strip() == "---":
            return (
                lines[0],
                "".join(lines[1:index]),
                line if line.endswith("\n") else line + "\n",
                "".join(lines[index + 1 :]),
            )
    raise StateError("the rule file does not close its frontmatter")


def _set_key(frontmatter: str, key: str, value: Any, file: Path) -> str:
    """``frontmatter`` with one top-level key replaced or added, the rest kept.

    The key's own lines are removed and the new value appended, so every
    other line keeps its bytes. The result is read back: if it does not hold
    exactly the old values with the new one, the write is refused.
    """
    try:
        before = yaml.safe_load(frontmatter) if frontmatter.strip() else {}
    except yaml.YAMLError as error:
        raise StateError(f"invalid frontmatter in {file}: {error}") from error
    if not isinstance(before, dict):
        raise StateError(f"the frontmatter of {file} is not a mapping")
    kept: list[str] = []
    skipping = False
    start = re.compile(rf"{re.escape(key)}\s*:")
    for line in frontmatter.splitlines(keepends=True):
        if start.match(line):
            skipping = True
            continue
        if skipping and line.strip() and (line[0] in " \t" or line.startswith("- ")):
            continue
        skipping = False
        kept.append(line)
    if kept and not kept[-1].endswith("\n"):
        kept[-1] += "\n"
    result = "".join(kept) + _dump({key: value})
    expected = {**{k: v for k, v in before.items() if k != key}, key: value}
    try:
        after = yaml.safe_load(result)
    except yaml.YAMLError:
        after = None
    if after != expected:
        raise StateError(
            f"ww cannot change {key} in the frontmatter of {file} without "
            "disturbing the rest; change it by hand"
        )
    return result


def _glob_report(project: RuleProject, paths: tuple[str, ...]) -> tuple[str, ...]:
    if not paths:
        return ()
    files = project_files(project.root)
    return tuple(
        f"`{glob}` matches {len(select_files(files, (glob,)))} file(s) now."
        for glob in paths
    )


def _glob_warnings(project: RuleProject, paths: tuple[str, ...]) -> tuple[str, ...]:
    if not paths:
        return ()
    files = project_files(project.root)
    return tuple(
        f"`{glob}` matches no file in the project, so the rule applies to no "
        "file until one exists"
        for glob in paths
        if not select_files(files, (glob,))
    )


def _wording_warnings(
    automation: RuleAutomation, rule: RuleDefinition, body: str
) -> tuple[str, ...]:
    new_hash = rule_text_hash(body)
    if new_hash == rule.text_hash:
        return ()
    warnings = [
        f"The wording changes, so its hash changes from {rule.text_hash[:12]} to "
        f"{new_hash[:12]}."
    ]
    entry = automation.rules.get(rule.text_hash)
    if entry is not None:
        warnings.append(
            f"The rule-automation store entry {rule.text_hash[:12]} ({entry.status}) "
            "stops matching this rule; once no rule has the old wording, "
            "`rules prune` removes it."
        )
        if entry.status == "converted" and entry.check is not None:
            warnings.append(
                f"Its approved check `{entry.check}` stops running for this rule. To "
                f"keep the command, put the old wording back, run `rules promote "
                f"{entry.check}`, then edit; otherwise a verifier judges the new "
                "wording and may propose a check again."
            )
    return tuple(warnings)


@dataclass(frozen=True)
class _RulesImport:
    """``ww-rules.yaml``: the rule groups ww writes, and whether it is imported."""

    path: Path
    label: str
    groups: dict[str, Any]
    other: dict[str, Any]
    imported: bool
    root_text: str
    root_raw: dict[str, Any]

    @classmethod
    def read(cls, project: RuleProject) -> _RulesImport:
        path = project.base / RULES_IMPORT_FILE
        label = project.label(path)
        root_text = project.config_path.read_text(encoding="utf-8")
        root_raw = yaml.safe_load(root_text)
        if not isinstance(root_raw, dict):  # pragma: no cover - it loaded
            raise StateError(f"{project.label(project.config_path)} is not a mapping")
        imports = root_raw.get("imports") or []
        imported = isinstance(imports, list) and any(
            isinstance(entry, str)
            and (project.base / entry).resolve() == path.resolve()
            for entry in imports
        )
        groups: dict[str, Any] = {}
        other: dict[str, Any] = {}
        if path.exists():
            if not imported:
                raise StateError(
                    f"{label} exists but {project.label(project.config_path)} does "
                    "not import it; add it to imports or move it away first"
                )
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if not isinstance(raw, dict) or not isinstance(raw.get("rules", {}), dict):
                raise StateError(f"{label} must hold a `rules` mapping")
            groups = dict(raw.get("rules") or {})
            other = {key: value for key, value in raw.items() if key != "rules"}
        return cls(path, label, groups, other, imported, root_text, root_raw)

    def write(self, groups: dict[str, Any]) -> FileWrite:
        return FileWrite(
            self.path, _IMPORT_HEADER + _dump({**self.other, "rules": groups})
        )

    def import_it(self, project: RuleProject) -> FileWrite:
        """The repo file with this file added to its imports, nothing else."""
        entry = RULES_IMPORT_FILE
        text = _with_import(self.root_text, self.root_raw, entry)
        imports = list(self.root_raw.get("imports") or [])
        expected = {"imports": [*imports, entry]}
        expected.update(
            (key, value) for key, value in self.root_raw.items() if key != "imports"
        )
        if yaml.safe_load(text) != expected:
            raise StateError(
                f"ww cannot add {entry} to the imports of "
                f"{project.label(project.config_path)} without changing anything "
                "else; add it by hand and run the command again"
            )
        return FileWrite(project.config_path, text)


def _with_import(text: str, raw: dict[str, Any], entry: str) -> str:
    """``text`` with ``entry`` appended to its top-level ``imports`` list.

    Without ``imports``, the list goes before the first top-level key, since
    imports come before every key but ``extends``. An existing list gains one
    line in block style, or one element in a one-line flow list.
    """
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    if "imports" not in raw:
        index = next(
            (
                position
                for position, line in enumerate(lines)
                if line.strip()
                and not line.startswith(("#", "---", "%", " ", "\t"))
            ),
            len(lines),
        )
        return "".join([*lines[:index], f"imports:\n  - {entry}\n", *lines[index:]])
    start = next(
        (
            position
            for position, line in enumerate(lines)
            if re.match(r"imports\s*:", line)
        ),
        None,
    )
    if start is None:
        return text
    value = lines[start].split(":", 1)[1].split(" #", 1)[0].strip()
    if value.startswith("[") and value.endswith("]"):
        line = lines[start]
        close = line.rindex("]")
        separator = ", " if value[1:-1].strip() else ""
        lines[start] = f"{line[:close]}{separator}{entry}{line[close:]}"
        return "".join(lines)
    if value:
        return text
    last = None
    prefix = "  - "
    for position in range(start + 1, len(lines)):
        line = lines[position]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.lstrip().startswith("- ") and (line[0] in " \t-"):
            last = position
            prefix = line[: len(line) - len(line.lstrip())] + "- "
            continue
        break
    if last is None:
        return text
    return "".join([*lines[: last + 1], f"{prefix}{entry}\n", *lines[last + 1 :]])

