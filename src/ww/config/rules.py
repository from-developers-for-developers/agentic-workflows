# SPDX-License-Identifier: GPL-3.0-or-later
"""Rule files, the root ``rules`` mapping, and a step's ``rules`` list.

A rule is a sentence a step's agent must follow, stored as the body of a
Markdown file whose optional YAML frontmatter scopes it to files (``paths``),
gives it a command ww runs when the step completes (``check``), and asks for
a worker to judge it (``agent``, ``model``, ``reasoning``). The root
``rules`` mapping names groups of rule files; a step's ``rules`` list adds its
own rules and names groups it wants regardless of their filters.

The text normalisation and hash defined here are the single source of truth
for a rule's identity by wording.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from ww.actions import Commands
from ww.actions.command import CommandAction
from ww.errors import ConfigurationError
from ww.extensions.api import RuleGroupContribution
from ww.validation import is_positive_int
from ww.workflow_config import (
    ALL,
    ALL_NAMES,
    NO_NAMES,
    HandlerDefinition,
    ItemFlow,
    NameFilter,
    RuleDefinition,
    RuleGroup,
    RuleGroupRef,
    RuleHints,
    StepDefinition,
    StepRule,
    UnresolvedStepRule,
    WorkflowDefinition,
)

from .values import (
    _NAME,
    _mapping,
    _name_filter,
    _only,
    _optional_agent,
    _optional_string,
)

RULE_FILE_KEYS = {
    "paths",
    "contains_in_file",
    "contains_in_diff",
    "check",
    "max_fixes",
    "agent",
    "model",
    "reasoning",
}
STEP_RULE_KEYS = {
    "text",
    "argv",
    "shell",
    "args",
    "env",
    "assert",
    "files",
    "max_fixes",
    "agent",
    "model",
    "reasoning",
}
_CHECK_KEYS = {"argv", "shell", "args", "env", "assert"}
# Beside the command keys on a rule's own check: the files it needs.
_CHECK_FILES_KEY = "files"
_GROUP_KEYS = {"rules", "workflows", "steps", "agent", "model", "reasoning"}
# A bare string shaped like this names a group or a file, never a sentence:
# "python" and "rules/python.md" match, "Write tests first" does not.
_REFERENCE = re.compile(r"[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)?")
# The end of a sentence: ".", "!" or "?" before a space or the end, e.g. the
# "." in "Run tests. Then lint." but not the one in "setup.py".
_SENTENCE_END = re.compile(r"[.!?](?=\s|$)")
# A run of whitespace, e.g. the "  \n " between two words.
_WHITESPACE = re.compile(r"\s+")


def normalize_rule_text(text: str) -> str:
    """The wording a rule is identified by: stripped, whitespace collapsed."""
    return _WHITESPACE.sub(" ", text.strip())


def rule_text_hash(text: str) -> str:
    """The sha256 of a rule's normalised text, the key of derived knowledge."""
    return hashlib.sha256(normalize_rule_text(text).encode("utf-8")).hexdigest()


def rule_summary(text: str) -> str:
    """The first sentence of ``text``, or its first line when it has none."""
    stripped = text.strip()
    end = _SENTENCE_END.search(stripped)
    first = stripped[: end.end()] if end else stripped.splitlines()[0]
    return normalize_rule_text(first)


def rule_source(source: str | None, root: Path) -> str | None:
    """A rule file as ww shows it: relative to ``root`` when it lies inside."""
    if source is None:
        return None
    path = Path(source)
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path)


def looks_like_reference(value: str) -> bool:
    """Whether a bare step entry names a group or file rather than a sentence."""
    return bool(_REFERENCE.fullmatch(value)) and not value.endswith(
        (".", "!", "?", ",", ";", ":")
    )


@dataclass(frozen=True)
class _DeclaredGroup:
    """One group as written, before its items are resolved.

    A string item is a group name, else a path relative to ``base``; a
    ``Path`` item is always a path. ``base`` is ``None`` for an extension's
    group, whose strings may only name groups.
    """

    name: str
    items: tuple[Path | str, ...]
    workflows: NameFilter
    steps: NameFilter
    hints: RuleHints
    origin: str
    base: Path | None


def parse_rule_file(
    path: Path, rule_id: str, hints: RuleHints | None = None
) -> RuleDefinition:
    """Read one rule file: optional YAML frontmatter, then the rule's body.

    ``hints`` are the group's; the file's own ``agent``, ``model``, and
    ``reasoning`` replace them field by field.
    """
    hints = hints if hints is not None else RuleHints()
    label = str(path)
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ConfigurationError(f"cannot read rule file {label}: {error}") from error
    frontmatter, body = _split_frontmatter(content, label)
    text = body.strip()
    if not text:
        raise ConfigurationError(f"rule file {label} has no rule text")
    context = f"rule file {label}"
    if "contains" in frontmatter:
        raise ConfigurationError(
            f"{context}.contains is not a key: use contains_in_file for strings "
            "the file's text holds, or contains_in_diff for strings in the "
            "lines the step changed"
        )
    _only(frontmatter, RULE_FILE_KEYS, context)
    check, check_files = (
        _parse_check(_mapping(frontmatter["check"], f"{context}.check"), context)
        if "check" in frontmatter
        else (None, ())
    )
    return RuleDefinition(
        id=rule_id,
        text=text,
        summary=rule_summary(text),
        text_hash=rule_text_hash(text),
        paths=(
            _paths(frontmatter.get("paths"), context) if "paths" in frontmatter else ()
        ),
        contains_in_file=_contains(frontmatter, "contains_in_file", context),
        contains_in_diff=_contains(frontmatter, "contains_in_diff", context),
        check=check,
        check_files=check_files,
        max_fixes=_max_fixes(frontmatter, context),
        hints=hints.overlay(_hints(frontmatter, context)),
        source=label,
    )


def _split_frontmatter(content: str, label: str) -> tuple[dict[str, Any], str]:
    lines = content.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return {}, content
    for index, line in enumerate(lines[1:], 1):
        if line.strip() == "---":
            raw = "".join(lines[1:index])
            try:
                data = yaml.safe_load(raw) if raw.strip() else {}
            except yaml.YAMLError as error:
                raise ConfigurationError(
                    f"invalid frontmatter in rule file {label}: {error}"
                ) from error
            if not isinstance(data, dict):
                raise ConfigurationError(
                    f"frontmatter of rule file {label} must be a mapping"
                )
            return data, "".join(lines[index + 1 :])
    raise ConfigurationError(f"rule file {label} does not close its frontmatter")


def _paths(value: Any, context: str) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item.strip() for item in value)
    ):
        raise ConfigurationError(f"{context}.paths must be a non-empty list of globs")
    return tuple(value)


def _contains(mapping: dict[str, Any], key: str, context: str) -> tuple[str, ...]:
    if key not in mapping:
        return ()
    value = mapping[key]
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item for item in value)
    ):
        raise ConfigurationError(
            f"{context}.{key} must be a non-empty list of non-empty strings"
        )
    return tuple(value)


def _max_fixes(mapping: dict[str, Any], context: str) -> int | None:
    if "max_fixes" not in mapping:
        return None
    value = mapping["max_fixes"]
    if not is_positive_int(value):
        raise ConfigurationError(f"{context}.max_fixes must be a positive integer")
    return int(value)


def _hints(mapping: dict[str, Any], context: str) -> RuleHints:
    return RuleHints(
        _optional_agent(mapping, "agent", context),
        _optional_string(mapping, "model", context),
        _optional_string(mapping, "reasoning", context),
    )


def parse_check_command(mapping: dict[str, Any], context: str) -> Commands:
    """A check: the cli handler's ``argv`` or ``shell`` shape and ``assert``."""
    for key in ("idempotent", "command"):
        if key in mapping:
            raise ConfigurationError(f"{context}.{key} is not allowed on a check")
    _only(mapping, _CHECK_KEYS, context)
    return CommandAction().parse(mapping, "check", "", context)


def _parse_check(
    mapping: dict[str, Any], context: str
) -> tuple[Commands, tuple[str, ...]]:
    """A rule's own check: its command, and the ``files`` it says it needs."""
    files = (
        _check_files(mapping[_CHECK_FILES_KEY], context)
        if _CHECK_FILES_KEY in mapping
        else ()
    )
    command = {key: value for key, value in mapping.items() if key != _CHECK_FILES_KEY}
    return parse_check_command(command, context), files


def _check_files(value: Any, context: str) -> tuple[str, ...]:
    """Paths inside the step's directory: relative, without a ``..`` part."""
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item.strip() for item in value)
    ):
        raise ConfigurationError(
            f"{context}.{_CHECK_FILES_KEY} must be a non-empty list of paths"
        )
    for item in value:
        parts = PurePosixPath(item).parts
        if PurePosixPath(item).is_absolute() or ".." in parts:
            raise ConfigurationError(
                f"{context}.{_CHECK_FILES_KEY}: {item!r} must stay inside the "
                "step's directory"
            )
    return tuple(value)


def parse_rules_root(
    raw: Any,
    base: Path,
    extension_groups: tuple[tuple[str, RuleGroupContribution], ...] = (),
) -> tuple[RuleGroup, ...]:
    """Resolve the root ``rules`` mapping and extension groups into groups.

    ``base`` is the directory relative item paths resolve against.
    Extension groups, each paired with the extension that ships it, come
    first; a name declared twice is an error. Items resolve in order: a group
    name, else a file or a directory of ``*.md`` files. Groups may name each
    other; a rule reached through a named group keeps that group's ID.
    """
    declared: dict[str, _DeclaredGroup] = {}
    for origin, contribution in extension_groups:
        if contribution.name in declared:
            raise ConfigurationError(
                f"rule group {contribution.name!r} is shipped by both "
                f"{declared[contribution.name].origin} and {origin}"
            )
        declared[contribution.name] = _DeclaredGroup(
            contribution.name,
            contribution.items,
            _contributed_filter(contribution.workflows),
            _contributed_filter(contribution.steps),
            contribution.hints,
            f"extension {origin}",
            None,
        )
    if raw is not None:
        if not isinstance(raw, dict):
            raise ConfigurationError("rules must be a mapping of group names")
        for name, value in raw.items():
            if not isinstance(name, str) or not _NAME.fullmatch(name):
                raise ConfigurationError("rules keys must be normalized group names")
            if name in declared:
                raise ConfigurationError(
                    f"rule group {name!r} is declared in the configuration and "
                    f"shipped by {declared[name].origin}"
                )
            declared[name] = _declared_group(name, value, base)
    return _GroupResolver(declared).resolve_all()


def _declared_group(name: str, value: Any, base: Path) -> _DeclaredGroup:
    path = f"rules.{name}"
    if isinstance(value, list):
        mapping: dict[str, Any] = {"rules": value}
    elif isinstance(value, dict):
        mapping = value
        _only(mapping, _GROUP_KEYS, path)
    else:
        raise ConfigurationError(f"{path} must be a list of items or a mapping")
    items = mapping.get("rules")
    if not isinstance(items, list) or not items:
        raise ConfigurationError(f"{path}.rules must be a non-empty list")
    for index, item in enumerate(items):
        if not isinstance(item, str) or not item.strip():
            raise ConfigurationError(
                f"{path}.rules[{index}] must be a non-empty string"
            )
    return _DeclaredGroup(
        name,
        tuple(items),
        _name_filter(
            mapping.get("workflows", ALL_NAMES), f"{path}.workflows", empty=NO_NAMES
        ),
        _name_filter(mapping.get("steps", ALL_NAMES), f"{path}.steps", empty=NO_NAMES),
        _hints(mapping, path),
        "configuration",
        base,
    )


def _contributed_filter(names: tuple[str, ...] | None) -> NameFilter:
    """An extension group's filter: ``None`` admits all, a tuple its names."""
    return ALL if names is None else NameFilter(names)


class _GroupResolver:
    """Resolve group items once each, following references and detecting cycles."""

    def __init__(self, declared: dict[str, _DeclaredGroup]) -> None:
        self.declared = declared
        self.resolved: dict[str, RuleGroup] = {}
        # One parse per file and group hints; copies change only the ID.
        self._parsed: dict[tuple[Path, RuleHints], RuleDefinition] = {}

    def resolve_all(self) -> tuple[RuleGroup, ...]:
        return tuple(self.resolve(name, ()) for name in self.declared)

    def resolve(self, name: str, chain: tuple[str, ...]) -> RuleGroup:
        if name in self.resolved:
            return self.resolved[name]
        if name in chain:
            raise ConfigurationError("rule group cycle: " + " -> ".join((*chain, name)))
        group = self.declared[name]
        rules: list[RuleDefinition] = []
        stems: dict[str, Path] = {}
        for item in group.items:
            if isinstance(item, str) and item in self.declared:
                rules.extend(self.resolve(item, (*chain, name)).rules)
                continue
            if isinstance(item, Path):
                path: Path | None = item
            else:
                path = group.base / item if group.base is not None else None
            if path is None or not path.exists():
                raise ConfigurationError(
                    f"rule group {name!r} item {str(item)!r} names no group or file"
                )
            for file in rule_files(path):
                other = stems.get(file.stem)
                if other is not None and other != file:
                    raise ConfigurationError(
                        f"rule group {name!r} has two rules named {file.stem!r}: "
                        f"{other} and {file}"
                    )
                stems[file.stem] = file
                rules.append(self._rule(file, f"{name}/{file.stem}", group.hints))
        self.resolved[name] = RuleGroup(
            name=name,
            rules=_unique_ids(rules),
            workflows=group.workflows,
            steps=group.steps,
            hints=group.hints,
            origin=group.origin,
        )
        return self.resolved[name]

    def _rule(self, file: Path, rule_id: str, hints: RuleHints) -> RuleDefinition:
        key = (file.resolve(), hints)
        parsed = self._parsed.get(key)
        if parsed is None:
            parsed = parse_rule_file(file, rule_id, hints)
            self._parsed[key] = parsed
        return replace(parsed, id=rule_id)


def rule_files(path: Path) -> tuple[Path, ...]:
    """A rule file itself, or every ``*.md`` directly inside a directory, sorted."""
    if path.is_dir():
        return tuple(sorted(child for child in path.glob("*.md") if child.is_file()))
    return (path,)


def _unique_ids(rules: list[RuleDefinition]) -> tuple[RuleDefinition, ...]:
    """Keep the first rule of each ID; nested groups may reach one twice."""
    first: dict[str, RuleDefinition] = {}
    for rule in rules:
        first.setdefault(rule.id, rule)
    return tuple(first.values())


def parse_step_rules(value: Any, step_name: str, path: str) -> tuple[StepRule, ...]:
    """Parse a step's ``rules`` list; bare strings are resolved later.

    A mapping is a rule of the step's own: ``text``, a command, or both. A
    bare string waits for :func:`resolve_step_rules`, which knows the groups
    and the configuration's directory.
    """
    if not isinstance(value, list):
        raise ConfigurationError(f"{path}.rules must be a list")
    result: list[StepRule] = []
    for index, entry in enumerate(value, 1):
        entry_path = f"{path}.rules[{index - 1}]"
        if isinstance(entry, str):
            if not entry.strip():
                raise ConfigurationError(f"{entry_path} must be a non-empty string")
            result.append(UnresolvedStepRule(entry.strip(), step_name, index))
        elif isinstance(entry, dict):
            result.append(_step_rule_mapping(entry, f"{step_name}/{index}", entry_path))
        else:
            raise ConfigurationError(f"{entry_path} must be a string or a mapping")
    return tuple(result)


def _step_rule_mapping(
    mapping: dict[str, Any], rule_id: str, path: str
) -> RuleDefinition:
    _only(mapping, STEP_RULE_KEYS, path)
    text = mapping.get("text")
    if text is not None and (not isinstance(text, str) or not text.strip()):
        raise ConfigurationError(f"{path}.text must be a non-empty string")
    command_keys = {
        key: mapping[key] for key in (*_CHECK_KEYS, _CHECK_FILES_KEY) if key in mapping
    }
    check, check_files = (
        _parse_check(command_keys, path) if command_keys else (None, ())
    )
    if text is None and check is None:
        raise ConfigurationError(f"{path} requires text or a command")
    wording = text.strip() if text is not None else _command_text(check)
    return RuleDefinition(
        id=rule_id,
        text=wording,
        summary=rule_summary(wording),
        text_hash=rule_text_hash(wording),
        check=check,
        check_files=check_files,
        max_fixes=_max_fixes(mapping, path),
        hints=_hints(mapping, path),
    )


def _command_text(check: Commands | None) -> str:
    """A pure check's text: the command it runs, as the rule's summary."""
    assert check is not None
    command = check.commands[0]
    return command.shell if command.shell is not None else " ".join(command.argv)


def resolve_step_rules(
    workflows: tuple[WorkflowDefinition, ...],
    handlers: tuple[HandlerDefinition, ...],
    groups: tuple[RuleGroup, ...],
    base: Path,
) -> tuple[tuple[WorkflowDefinition, ...], tuple[HandlerDefinition, ...]]:
    """Resolve every bare string in every step's ``rules`` list.

    A string names a group, else a rule file or directory relative to
    ``base``, else it is the rule's text. A string shaped like a reference
    that names neither a group nor a file is an error, so a mistyped group
    name is not silently delivered as a rule.
    """
    names = {group.name for group in groups}

    def resolve(step: StepDefinition) -> StepDefinition:
        rules = tuple(
            resolved
            for entry in step.rules
            for resolved in _resolve_entry(entry, names, base)
        )
        return replace(
            step,
            rules=rules,
            child_steps=tuple(map(resolve, step.child_steps)),
            assessment_outcomes=tuple(map(resolve, step.assessment_outcomes)),
            items=(
                replace(step.items, steps=tuple(map(resolve, step.items.steps)))
                if isinstance(step.items, ItemFlow)
                else step.items
            ),
        )

    return (
        tuple(
            replace(workflow, steps=tuple(map(resolve, workflow.steps)))
            for workflow in workflows
        ),
        tuple(
            resolve(handler) if isinstance(handler, StepDefinition) else handler
            for handler in handlers
        ),
    )


def _resolve_entry(
    entry: StepRule, group_names: set[str], base: Path
) -> tuple[RuleDefinition | RuleGroupRef, ...]:
    if not isinstance(entry, UnresolvedStepRule):
        return (entry,)
    value = entry.value
    if value in group_names:
        return (RuleGroupRef(value),)
    candidate = base / value
    if candidate.exists():
        return tuple(
            parse_rule_file(file, f"{entry.step}/{file.stem}")
            for file in rule_files(candidate)
        )
    if looks_like_reference(value):
        raise ConfigurationError(
            f"step {entry.step!r} rule {value!r} names no group or file"
        )
    return (
        RuleDefinition(
            id=f"{entry.step}/{entry.ordinal}",
            text=value,
            summary=rule_summary(value),
            text_hash=rule_text_hash(value),
        ),
    )
