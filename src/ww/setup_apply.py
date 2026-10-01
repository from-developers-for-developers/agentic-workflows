# SPDX-License-Identifier: GPL-3.0-or-later
"""``ww setup apply``: place a proposed configuration fragment where it belongs.

A setup skill proposes a fragment: any of the root keys ``workflows``,
``modes``, ``profiles``, ``documents``, ``handlers``, ``hooks`` and ``rules``,
plus an optional ``settings`` mapping of ``ww-agentic-workflows.json`` keys.
ww, not the agent, puts it in place, so no agent edits ww's own configuration
files (which some agents' safety layers refuse):

- ``--for team``: the YAML part goes into ``ww-setup.yaml`` next to the repo
  file, which lists it under ``imports``; ``settings`` merge into
  ``ww-agentic-workflows.json``;
- ``--for me``: the YAML part goes into ``ww-setup.local.yaml``, imported by
  ``ww-agentic-workflows.local.yaml`` (created with just that import when it
  is missing); ``settings`` merge into ``ww-agentic-workflows.local.json``. All
  three stay out of version control.

The setup file is ww's own and rewritten whole: a definition of the same name
replaces the one already there (rule groups whole, hooks appended), and the
rest are kept. Adding the import is the only change to a root file, checked to
leave everything else as it was. Settings merge key by key, and a key that
already holds a different value is a conflict: the whole apply is refused,
listing each one.

The plan is validated in memory: the configuration is loaded as ww would,
reading the planned files' new contents in place of the files on disk, so
validating never touches the project. Only a plan that would load is shown,
and the files are written once, after the operator confirms; a failed write
puts back every file already written.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ww.config import load_configuration
from ww.config.composition import entry_name
from ww.config_files import (
    LOCAL_SETTINGS_FILE,
    LOCAL_SETUP_IMPORT_FILE,
    LOCAL_WORKFLOWS_FILE,
    SETTINGS_FILE,
    SETUP_IMPORT_FILE,
    display_path,
    staged_files,
)
from ww.config_writes import FileWrite, Transaction, dump_yaml, import_write
from ww.errors import StateError, WwError
from ww.extensions import ExtensionRegistry

AUDIENCES = ("me", "team")
SETTINGS_KEY = "settings"
# The named catalogs: an entry replaces the one of the same name.
NAMED = {
    "workflows": "workflow",
    "modes": "mode",
    "documents": "document",
    "handlers": "handler",
}
# Mappings whose keys name what they hold: a key replaces the same key.
KEYED = {"profiles": "profile", "rules": "rule group"}
HOOKS = "hooks"
FRAGMENT_KEYS = (*NAMED, *KEYED, HOOKS)


@dataclass(frozen=True)
class FileChange:
    """One file the apply writes, and what it changes there."""

    path: Path
    created: bool
    details: tuple[str, ...]


@dataclass(frozen=True)
class SetupPlan:
    """Everything one apply writes, and what the operator is shown first."""

    audience: str
    writes: tuple[FileWrite, ...]
    changes: tuple[FileChange, ...]
    warnings: tuple[str, ...] = field(default=())


@dataclass(frozen=True)
class _Target:
    setup_file: Path
    # The root file whose ``imports`` list the setup file.
    root_file: Path
    settings_file: Path


def _target(config_path: Path, audience: str) -> _Target:
    base = config_path.parent
    if audience == "team":
        return _Target(base / SETUP_IMPORT_FILE, config_path, base / SETTINGS_FILE)
    return _Target(
        base / LOCAL_SETUP_IMPORT_FILE,
        base / LOCAL_WORKFLOWS_FILE,
        base / LOCAL_SETTINGS_FILE,
    )


def plan_setup(
    root: Path, config_path: Path, fragment: Path, audience: str
) -> SetupPlan:
    """Plan the writes placing ``fragment`` for ``audience``; nothing is written."""
    if audience not in AUDIENCES:
        raise StateError(f"--for takes {' or '.join(AUDIENCES)}")
    definitions, settings = _read_fragment(fragment)
    target = _target(config_path, audience)

    def label(path: Path) -> str:
        return display_path(path, root)

    writes: list[FileWrite] = []
    changes: list[FileChange] = []
    root_raw, root_text = _read_yaml(target.root_file, label(target.root_file))
    imported = any(
        isinstance(entry, str)
        and (target.root_file.parent / entry).resolve() == target.setup_file.resolve()
        for entry in (root_raw.get("imports") or [])
    )
    if target.setup_file.exists() and not imported:
        raise StateError(
            f"{label(target.setup_file)} exists but {label(target.root_file)} does "
            "not import it; add it to imports or move it away first"
        )
    existing, _ = _read_yaml(target.setup_file, label(target.setup_file))
    merged, details = _merge(existing, definitions)
    # A fragment whose every definition is already in the imported setup file,
    # hooks included, leaves that file as it is.
    if definitions and not (imported and merged == existing):
        header = (
            f"# Written by `ww setup apply --for {audience}`; ww rewrites this "
            f"file whole,\n# and {target.root_file.name} imports it.\n"
        )
        writes.append(FileWrite(target.setup_file, header + dump_yaml(merged)))
        changes.append(
            FileChange(target.setup_file, not target.setup_file.exists(), details)
        )
        if not imported:
            entry = target.setup_file.name
            if target.root_file.exists():
                writes.append(
                    import_write(
                        target.root_file,
                        root_text,
                        root_raw,
                        entry,
                        label(target.root_file),
                    )
                )
            else:
                writes.append(
                    FileWrite(target.root_file, f"imports:\n  - {entry}\n")
                )
            changes.append(
                FileChange(
                    target.root_file,
                    not target.root_file.exists(),
                    (f"adds {entry} to imports",),
                )
            )
    if settings:
        write, change = _settings_write(target.settings_file, settings, label)
        if write is not None and change is not None:
            writes.append(write)
            changes.append(change)
    if not writes:
        raise StateError(
            f"{label(fragment)} changes nothing: everything it proposes is "
            "already in place"
        )
    return SetupPlan(
        audience,
        tuple(writes),
        tuple(changes),
        _shadowed(definitions, root_raw, target, label),
    )


def validate_setup(root: Path, config_path: Path, plan: SetupPlan) -> None:
    """Load the configuration as the plan would leave it; nothing is written."""
    with staged_files({write.path: write.content for write in plan.writes}):
        try:
            load_configuration(config_path, ExtensionRegistry.discover(root))
        except WwError as error:
            raise StateError(
                f"refused: the configuration would not be valid ({error}); "
                "nothing was written"
            ) from error


def apply_setup(plan: SetupPlan) -> None:
    """Write the validated plan, every file or none."""
    with Transaction() as transaction:
        transaction.apply(plan.writes)


def render_plan(plan: SetupPlan, root: Path) -> str:
    audience = (
        "the team: files shared through the repository"
        if plan.audience == "team"
        else "you only: local files kept out of version control"
    )
    lines = [f"`ww setup apply --for {plan.audience}` writes for {audience}:"]
    for change in plan.changes:
        state = " (new)" if change.created else ""
        lines.append(
            f"- {display_path(change.path, root)}{state}: " + "; ".join(change.details)
        )
    lines.extend(f"Warning: {warning}" for warning in plan.warnings)
    return "\n".join(lines) + "\n"


def plan_to_dict(plan: SetupPlan, root: Path, *, applied: bool) -> dict[str, object]:
    return {
        "for": plan.audience,
        "files": [
            {
                "path": display_path(change.path, root),
                "created": change.created,
                "changes": list(change.details),
            }
            for change in plan.changes
        ],
        "warnings": list(plan.warnings),
        "applied": applied,
    }


def _read_fragment(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """The fragment's definitions and its settings, each checked for shape."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise StateError(f"cannot read {path}: {error}") from error
    except yaml.YAMLError as error:
        raise StateError(f"invalid YAML in {path}: {error}") from error
    if not isinstance(raw, dict) or not raw:
        raise StateError(f"{path} must be a mapping of configuration keys")
    unknown = set(raw) - {*FRAGMENT_KEYS, SETTINGS_KEY}
    if unknown:
        raise StateError(
            f"{path} has unknown key(s): {', '.join(sorted(map(str, unknown)))}; "
            f"a setup fragment holds {', '.join(FRAGMENT_KEYS)} and {SETTINGS_KEY}"
        )
    for key in NAMED:
        if key in raw:
            entries = raw[key]
            if not isinstance(entries, list):
                raise StateError(f"{path}: {key} must be a list")
            for index, entry in enumerate(entries):
                if entry_name(entry) is None:
                    raise StateError(f"{path}: {key}[{index}] needs a name")
    for key in (*KEYED, HOOKS, SETTINGS_KEY):
        if key in raw and not isinstance(raw[key], dict):
            raise StateError(f"{path}: {key} must be a mapping")
    for phase, entries in (raw.get(HOOKS) or {}).items():
        if not isinstance(entries, list):
            raise StateError(f"{path}: hooks.{phase} must be a list")
    settings = raw.pop(SETTINGS_KEY, None) or {}
    return raw, settings


def _read_yaml(path: Path, label: str) -> tuple[dict[str, Any], str]:
    if not path.exists():
        return {}, ""
    text = path.read_text(encoding="utf-8")
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise StateError(f"invalid YAML in {label}: {error}") from error
    if raw is None:
        return {}, text
    if not isinstance(raw, dict):
        raise StateError(f"{label} must contain a mapping")
    return raw, text


def _merge(
    existing: dict[str, Any], definitions: dict[str, Any]
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """The setup file with the fragment folded in, and what that changes."""
    merged = {key: value for key, value in existing.items()}
    added: list[str] = []
    replaced: list[str] = []
    appended: list[str] = []
    kept: list[str] = []
    for key, value in definitions.items():
        if key in NAMED:
            current = list(merged.get(key) or [])
            positions = {
                entry_name(entry): index for index, entry in enumerate(current)
            }
            for entry in value:
                name = entry_name(entry)
                if name in positions:
                    current[positions[name]] = entry
                    replaced.append(f"{NAMED[key]} `{name}`")
                else:
                    positions[name] = len(current)
                    current.append(entry)
                    added.append(f"{NAMED[key]} `{name}`")
            merged[key] = current
        elif key in KEYED:
            mapping = dict(merged.get(key) or {})
            for name, item in value.items():
                (replaced if name in mapping else added).append(
                    f"{KEYED[key]} `{name}`"
                )
                mapping[name] = item
            merged[key] = mapping
        else:
            hooks = dict(merged.get(HOOKS) or {})
            for phase, entries in value.items():
                # Hooks carry no name: an entry identical to one already in
                # the phase is the same hook, so applying twice adds it once.
                current = list(hooks.get(phase) or [])
                new = []
                for entry in entries:
                    if entry in current or entry in new:
                        continue
                    new.append(entry)
                hooks[phase] = [*current, *new]
                if new:
                    appended.append(_hooks_phrase(len(new), phase))
                if len(new) < len(entries):
                    skipped = len(entries) - len(new)
                    kept.append(
                        f"{skipped} hook{'s' if skipped != 1 else ''} already "
                        f"in {phase}"
                    )
            merged[HOOKS] = hooks
    details = [
        *(["adds " + ", ".join(added)] if added else []),
        *(["replaces " + ", ".join(replaced)] if replaced else []),
        *(["appends " + ", ".join(appended)] if appended else []),
        *(["skips " + ", ".join(kept)] if kept else []),
    ]
    return merged, tuple(details)


def _hooks_phrase(count: int, phase: str) -> str:
    return f"{count} hook{'s' if count != 1 else ''} to {phase}"


def _settings_write(
    path: Path, settings: dict[str, Any], label: Callable[[Path], str]
) -> tuple[FileWrite | None, FileChange | None]:
    """Merge ``settings`` into the JSON file key by key, refusing conflicts."""
    current: Any = {}
    if path.exists():
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise StateError(f"invalid {label(path)}: {error}") from error
        if not isinstance(current, dict):
            raise StateError(f"{label(path)} must contain a JSON object")
    added: list[str] = []
    conflicts: list[str] = []

    def merge(target: dict[str, Any], source: dict[str, Any], prefix: str) -> None:
        for key, value in source.items():
            dotted = f"{prefix}{key}"
            if key not in target:
                target[key] = value
                added.append(dotted)
            elif isinstance(target[key], dict) and isinstance(value, dict):
                merge(target[key], value, f"{dotted}.")
            elif target[key] != value:
                conflicts.append(
                    f"{dotted} is {json.dumps(target[key])}, the fragment "
                    f"proposes {json.dumps(value)}"
                )

    merge(current, settings, "")
    if conflicts:
        raise StateError(
            f"refused: {label(path)} already sets different values, and ww "
            "does not overwrite them; nothing was written:\n"
            + "\n".join(f"- {conflict}" for conflict in conflicts)
        )
    if not added:
        return None, None
    return (
        FileWrite(path, json.dumps(current, indent=2) + "\n"),
        FileChange(path, not path.exists(), ("sets " + ", ".join(added),)),
    )


def _shadowed(
    definitions: dict[str, Any],
    root_raw: dict[str, Any],
    target: _Target,
    label: Callable[[Path], str],
) -> tuple[str, ...]:
    """Fragment definitions the importing root file overrides.

    A level's imports fold before its root file, so a definition the root file
    also holds keeps the root's version.
    """
    warnings: list[str] = []
    for key, value in definitions.items():
        declared = root_raw.get(key)
        if key in NAMED and isinstance(declared, list):
            names = {entry_name(entry) for entry in declared}
            shadowed = [
                entry_name(entry) for entry in value if entry_name(entry) in names
            ]
            kind = NAMED[key]
        elif key in KEYED and isinstance(declared, dict):
            shadowed = [name for name in value if name in declared]
            kind = KEYED[key]
        else:
            continue
        warnings.extend(
            f"{kind} `{name}` is also defined in {label(target.root_file)}, which "
            f"takes precedence over {target.setup_file.name}; this one has no "
            "effect until it is removed there"
            for name in shadowed
        )
    return tuple(warnings)
