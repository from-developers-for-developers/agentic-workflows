# SPDX-License-Identifier: GPL-3.0-or-later
"""The rule-automation store: what ww learned about rules without a command.

A rule without a command is judged by a verifier agent, unless a converted
check covers its wording. ``ww-scriptize-rules`` builds such checks outside
any task, and ``ww rules convert`` records each on the operator's
confirmation. ww keeps that knowledge in ``ww-rule-automation.json`` at the
project root, a file meant to be committed so every checkout and task shares
it. Nothing here is configuration:
verification never rewrites YAML or rule files, and the store is derived
knowledge only; the operator's ``ww rules`` writes are the only way rule files
change (:mod:`ww.rule_writes`).

Two maps make up the store. ``rules`` is keyed by the hash of a rule's
normalised text (:func:`ww.config.rules.rule_text_hash`, the single source of
truth for that key), so the same wording shares its knowledge wherever it is
declared and a changed wording starts over. ``checks`` is keyed by a short
check name; one check may cover several rules, the
normal case for an ecosystem tool whose one configuration holds many rules.
Only a ``converted`` check is ever run, and a rule is mechanical only when
its entry is ``converted`` and names a ``converted`` check. A store written
before verifiers stopped proposing checks may still hold their interim
statuses (an approach, a proposed check, a pending revision); they are read
as they are and never written any more.

The file is shared by every task of the project, so each change is a
read-modify-write under a dedicated file lock, taken inside the task lock of
the command that changes it and never the other way round.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any, Literal, cast

from ww.actions import Commands
from ww.config.rules import parse_check_command
from ww.config_files import RULE_AUTOMATION_FILE
from ww.contracts import CheckAutomationStatus, RuleAutomationStatus
from ww.errors import ConfigurationError, StateError
from ww.locking import FileLocks
from ww.validation import (
    expect_bool,
    expect_literal,
    expect_optional_string,
    expect_string,
    is_strict_int,
)

STORE_FILE = RULE_AUTOMATION_FILE
STORE_SCHEMA_VERSION = 1
# Who approved a proposal. ww asks the operator for every approval; ``auto``
# is what a store written by an earlier ww may record for its own approvals.
RuleApprover = Literal["operator", "auto"]
# A check name: lower-case words joined by single hyphens, e.g. "lint-src".
CHECK_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
CHECK_NAME_LIMIT = 40
_RULE_KEYS = {
    "text",
    "status",
    "interpretation",
    "approach",
    "check",
    "extends",
    "reason",
    "candidates",
    "proposed_in",
    "proposed_run",
    "approved_by",
    "approved_in",
}
_APPROVAL_KEYS = {"proposed_run", "approved_by", "approved_in"}
_SPEC_KEYS = {"argv", "shell", "args", "env", "assert", "config", "covers", "proven"}
_CHECK_KEYS = (
    _SPEC_KEYS
    | {
        "status",
        "proposed_at",
        "approved_at",
        "proposed_in",
        "pending",
        "reason",
    }
    | _APPROVAL_KEYS
)


@dataclass(frozen=True)
class RuleEntry:
    """What ww knows about one rule wording.

    ``check`` names the check that covers the rule, or, for an approach, the
    check the verifier would create or extend (``extends``). ``reason``
    explains a ``not_convertible`` or ``rejected`` rule; ``candidates`` are
    the readings of an ``ambiguous`` one. ``proposed_in`` is the
    verification item that last reported on it, and ``proposed_run`` its run
    (``<task>/<run>``). ``approved_by`` and ``approved_in`` record who
    approved its approach, reading, or check, and in which run; ``None`` for
    an approval recorded before the store kept them.
    """

    text: str
    status: RuleAutomationStatus
    interpretation: str | None = None
    approach: str | None = None
    check: str | None = None
    extends: bool = False
    reason: str | None = None
    candidates: tuple[str, ...] = ()
    proposed_in: str | None = None
    proposed_run: str | None = None
    approved_by: RuleApprover | None = None
    approved_in: str | None = None

    def to_dict(self) -> dict[str, object]:
        data: dict[str, object] = {"text": self.text, "status": self.status}
        for key, value in (
            ("interpretation", self.interpretation),
            ("approach", self.approach),
            ("check", self.check),
            ("reason", self.reason),
            ("proposed_in", self.proposed_in),
            ("proposed_run", self.proposed_run),
            ("approved_by", self.approved_by),
            ("approved_in", self.approved_in),
        ):
            if value is not None:
                data[key] = value
        if self.extends:
            data["extends"] = True
        if self.candidates:
            data["candidates"] = list(self.candidates)
        return data

    @classmethod
    def from_dict(cls, data: Any, path: str) -> RuleEntry:
        mapping = _mapping(data, path, _RULE_KEYS)
        return cls(
            text=expect_string(mapping.get("text"), f"{path}.text"),
            status=expect_literal(
                mapping.get("status"), RuleAutomationStatus, f"{path}.status"
            ),
            interpretation=expect_optional_string(
                mapping.get("interpretation"), f"{path}.interpretation"
            ),
            approach=expect_optional_string(
                mapping.get("approach"), f"{path}.approach"
            ),
            check=expect_optional_string(mapping.get("check"), f"{path}.check"),
            extends=expect_bool(mapping.get("extends", False), f"{path}.extends"),
            reason=expect_optional_string(mapping.get("reason"), f"{path}.reason"),
            candidates=_strings(mapping.get("candidates", []), f"{path}.candidates"),
            proposed_in=expect_optional_string(
                mapping.get("proposed_in"), f"{path}.proposed_in"
            ),
            **_approval(mapping, path),
        )


def _approval(mapping: dict[str, Any], path: str) -> dict[str, Any]:
    """The provenance fields an entry shares: its run, approver, approval run."""
    approver = mapping.get("approved_by")
    return {
        "proposed_run": expect_optional_string(
            mapping.get("proposed_run"), f"{path}.proposed_run"
        ),
        "approved_by": (
            expect_literal(approver, RuleApprover, f"{path}.approved_by")
            if approver is not None
            else None
        ),
        "approved_in": expect_optional_string(
            mapping.get("approved_in"), f"{path}.approved_in"
        ),
    }


@dataclass(frozen=True)
class CheckSpec:
    """One derived check: its command, the files holding its logic, its rules.

    ``command`` is the cli handler shape (``argv`` or ``shell`` with ``args``
    and ``env``, and ``assert``); ``config`` lists the project files that
    carry the check's logic, so a later rule can join the same tool;
    ``covers`` are the text hashes of the rules it checks; ``proven`` is the
    verifier's report that the check fails on a deliberate violation and
    passes on the real change set.
    """

    command: Commands
    config: tuple[str, ...] = ()
    covers: tuple[str, ...] = ()
    proven: bool = False

    def to_dict(self) -> dict[str, object]:
        data: dict[str, object] = dict(self.command.commands[0].to_dict())
        data["assert"] = (
            self.command.assertion.to_data() if self.command.assertion else None
        )
        data["config"] = list(self.config)
        data["covers"] = list(self.covers)
        data["proven"] = self.proven
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any], path: str) -> CheckSpec:
        command_keys = cast(
            dict[str, Any],
            {
                key: data[key]
                for key in ("argv", "shell", "args", "env", "assert")
                if key in data and data[key] is not None
            },
        )
        return cls(
            command=parse_command(command_keys, path),
            config=_strings(data.get("config", []), f"{path}.config"),
            covers=_strings(data.get("covers", []), f"{path}.covers"),
            proven=expect_bool(data.get("proven", False), f"{path}.proven"),
        )


def is_check_name(value: str) -> bool:
    """Whether ``value`` is a check name: kebab-case, at most 40 characters.

    The limit keeps a name from ever looking like a rule's 64-character hash.
    """
    return len(value) <= CHECK_NAME_LIMIT and CHECK_NAME.fullmatch(value) is not None


def is_config_path(path: str) -> bool:
    """Whether ``path`` stays inside the directory it is read from."""
    pure = PurePosixPath(path)
    return bool(path.strip()) and not pure.is_absolute() and ".." not in pure.parts


def parse_command(mapping: dict[str, Any], path: str) -> Commands:
    """A check command in the cli handler shape; one command, no ``idempotent``."""
    try:
        return parse_check_command(mapping, path)
    except ConfigurationError as error:
        raise StateError(str(error)) from error


def describe_command(command: Commands) -> str:
    """A check command as the operator reads it before approving it."""
    first = command.commands[0]
    if first.shell is not None:
        text = first.shell
        if first.args:
            text += "  (args: " + " ".join(first.args) + ")"
        if first.env:
            text += "  (env: " + ", ".join(f"{k}={v}" for k, v in first.env) + ")"
    else:
        text = " ".join(first.argv)
    return text


@dataclass(frozen=True)
class CheckEntry:
    """One derived check and its approval state.

    ``pending`` is a revision of a ``converted`` check that a verifier once
    proposed, as only an old store holds; it never runs, and ``rules
    convert`` drops it. ``reason`` explains a
    ``rejected`` check, such as the operator's ``ww rules revoke``. The
    provenance fields are those of :class:`RuleEntry`.
    """

    spec: CheckSpec
    status: CheckAutomationStatus
    proposed_at: str | None = None
    approved_at: str | None = None
    proposed_in: str | None = None
    pending: CheckSpec | None = None
    reason: str | None = None
    proposed_run: str | None = None
    approved_by: RuleApprover | None = None
    approved_in: str | None = None

    def to_dict(self) -> dict[str, object]:
        data = self.spec.to_dict()
        data["status"] = self.status
        for key, value in (
            ("proposed_at", self.proposed_at),
            ("approved_at", self.approved_at),
            ("proposed_in", self.proposed_in),
            ("reason", self.reason),
            ("proposed_run", self.proposed_run),
            ("approved_by", self.approved_by),
            ("approved_in", self.approved_in),
        ):
            if value is not None:
                data[key] = value
        if self.pending is not None:
            data["pending"] = self.pending.to_dict()
        return data

    @classmethod
    def from_dict(cls, data: Any, path: str) -> CheckEntry:
        mapping = _mapping(data, path, _CHECK_KEYS)
        pending = mapping.get("pending")
        return cls(
            spec=CheckSpec.from_dict(mapping, path),
            status=expect_literal(
                mapping.get("status"), CheckAutomationStatus, f"{path}.status"
            ),
            proposed_at=expect_optional_string(
                mapping.get("proposed_at"), f"{path}.proposed_at"
            ),
            approved_at=expect_optional_string(
                mapping.get("approved_at"), f"{path}.approved_at"
            ),
            proposed_in=expect_optional_string(
                mapping.get("proposed_in"), f"{path}.proposed_in"
            ),
            pending=(
                CheckSpec.from_dict(
                    _mapping(pending, f"{path}.pending", _SPEC_KEYS), f"{path}.pending"
                )
                if pending is not None
                else None
            ),
            reason=expect_optional_string(mapping.get("reason"), f"{path}.reason"),
            **_approval(mapping, path),
        )


@dataclass(frozen=True)
class RuleAutomation:
    """The whole store. Its maps are owned by the instance and never mutated;
    ``with_rule`` and ``with_check`` return a changed copy."""

    rules: dict[str, RuleEntry] = field(default_factory=dict)
    checks: dict[str, CheckEntry] = field(default_factory=dict)

    def with_rule(self, text_hash: str, entry: RuleEntry) -> RuleAutomation:
        return replace(self, rules={**self.rules, text_hash: entry})

    def with_check(self, name: str, entry: CheckEntry) -> RuleAutomation:
        return replace(self, checks={**self.checks, name: entry})

    def converted_check(self, text_hash: str) -> tuple[str, CheckEntry] | None:
        """The converted check that makes a rule mechanical, if there is one."""
        entry = self.rules.get(text_hash)
        if entry is None or entry.status != "converted" or entry.check is None:
            return None
        check = self.checks.get(entry.check)
        if check is None or check.status != "converted":
            return None
        return entry.check, check

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": STORE_SCHEMA_VERSION,
            "rules": {key: entry.to_dict() for key, entry in self.rules.items()},
            "checks": {key: entry.to_dict() for key, entry in self.checks.items()},
        }

    @classmethod
    def from_dict(cls, data: Any) -> RuleAutomation:
        if not isinstance(data, dict):
            raise ValueError("the rule automation store must be an object")
        version = data.get("schema_version")
        if not is_strict_int(version) or version != STORE_SCHEMA_VERSION:
            raise ValueError(f"unsupported rule automation schema: {version!r}")
        unknown = set(data) - {"schema_version", "rules", "checks"}
        if unknown:
            raise ValueError(
                "unknown rule automation keys: " + ", ".join(sorted(unknown))
            )
        rules = data.get("rules", {})
        checks = data.get("checks", {})
        if not isinstance(rules, dict) or not isinstance(checks, dict):
            raise ValueError("rule automation rules and checks must be objects")
        for name in checks:
            if not isinstance(name, str) or not is_check_name(name):
                raise ValueError(f"invalid check name in the store: {name!r}")
        return cls(
            rules={
                expect_string(key, "rule hash"): RuleEntry.from_dict(
                    value, f"rules.{key}"
                )
                for key, value in rules.items()
            },
            checks={
                key: CheckEntry.from_dict(value, f"checks.{key}")
                for key, value in checks.items()
            },
        )


class RuleStore:
    """Load and change ``ww-rule-automation.json`` at the project root.

    A missing file is an empty store. An unreadable or malformed file is an
    error, never an empty store: losing approvals silently would re-ask the
    operator and could hide a decision.
    """

    def __init__(self, root: Path) -> None:
        self.path = Path(root) / STORE_FILE
        self.locks = FileLocks(Path(root))

    def exists(self) -> bool:
        return self.path.is_file()

    def load(self) -> RuleAutomation:
        if not self.path.exists():
            return RuleAutomation()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return RuleAutomation.from_dict(raw)
        except (OSError, json.JSONDecodeError, ValueError, StateError) as error:
            raise StateError(
                f"invalid rule automation store {self.path}: {error}"
            ) from error

    def modify(
        self, change: Callable[[RuleAutomation], RuleAutomation]
    ) -> RuleAutomation:
        """Apply ``change`` to the current store under its lock and save it.

        The whole read-modify-write holds the store's lock, so two tasks
        recording results at once never lose each other's entries. The file is
        written only when something changed.
        """
        with self.locks.lock(self.path, purpose="rule automation store"):
            current = self.load()
            updated = change(current)
            if updated != current:
                self.locks.atomic_write(
                    self.path, json.dumps(updated.to_dict(), indent=2) + "\n"
                )
            return updated


def _mapping(data: Any, path: str, allowed: set[str]) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError(f"{path} must be an object")
    unknown = set(data) - allowed
    if unknown:
        raise ValueError(f"{path} has unknown keys: " + ", ".join(sorted(unknown)))
    return data


def _strings(value: Any, path: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item for item in value
    ):
        raise ValueError(f"{path} must be a list of non-empty strings")
    return tuple(value)
