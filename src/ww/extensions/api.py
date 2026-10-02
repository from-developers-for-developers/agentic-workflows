# SPDX-License-Identifier: GPL-3.0-or-later
"""The contract a ww extension is written against.

An extension adds handlers, modes, commands, core-variable overrides, a
template namespace, and rule groups to ww. It is identified by a
``vendor/name`` pair and is addressed from ``ww.yaml`` by
its fully qualified reference, never by a bare name:

```yaml
hooks:
  after_complete:
    - name: ext/ww/git/handlers:git-commit
```

Nothing an extension provides takes effect until it is referenced that way, so
installing one can never change the meaning of a name the project already uses.

Variable overrides apply only when the extension is otherwise referenced by a
saved workflow plan. Extensions do not provide hooks. A hook decides *when*
work runs against a particular workflow and step, which is knowledge that
belongs to the project's configuration; an extension only supplies the work
itself.

Packaged extensions use a ``vendor.name`` entry-point name. Discovery reads
that metadata without importing the package; loading occurs only when the
extension is referenced. Extensions are trusted in-process Python once loaded.
``api_version`` declares compatibility with this module's
``EXTENSION_API_VERSION``; ``version`` identifies extension behavior in saved
plans.

Writing one
-----------

Create ``<project>/ext/<vendor>/<name>/extension.py`` exposing ``EXTENSION``, or
publish a package advertising an ``ww.extensions`` entry point that resolves to
an :class:`Extension`:

```python
from ww.extensions.api import Extension, ExtensionHandler, ExtensionResult
from ww.validation import is_strict_int

def _greet(context):
    return ExtensionResult(
        True,
        f"hello from {context.root}",
        values={"greeting": "hello"},
    )

EXTENSION = Extension(
    vendor="acme",
    name="hello",
    version="1.0.0",
    description="A minimal example.",
    handlers=(
        ExtensionHandler(
            "greet", _greet, "Say hello.", outputs=("greeting",)
        ),
    ),
)
```

Reserved paths
--------------

An extension that creates something outside ww's own state for a task, such
as a Git worktree, may declare ``reserved_paths``. ww asks configured
extensions for those paths before handing out a generated task ID, so an ID
whose worktree still exists on disk is never reused for an unrelated task.

Task records
------------

An extension that keeps its own records per task, as ww/git keeps branch and
commit records, may declare two hooks. ``claims_task`` returns whether the
extension still holds anything for ``task_id`` (a record, or a branch named
after the task); ww skips such an ID when it generates one, as it does for a
reserved path. ``forget_task`` drops the extension's records for ``task_id``
and its children; ``ww reset`` calls it for every configured extension, so a
reset task leaves nothing behind that could shape a later task under the same
ID.

Rule groups
-----------

An extension may ship rule groups in ``rules``: each a
:class:`RuleGroupContribution` naming the group, its rule files or
directories as absolute paths (``Path(__file__).parent / "rules"``) or other
group names, and optional ``workflows``/``steps`` filters. A project gets them
by listing the extension in its root settings ``extensions``, even with an
empty section; they come before the project's own groups, and a name the
project also declares is an error.

Template namespace
------------------

An extension may declare one ``namespace``: an :class:`ExtensionNamespace`
whose variables templates read as ``{{ww.<namespace>.<variable>}}``, for
example ww/git's ``{{ww.git.branch}}``. They are available to every workflow
of a project that configures the extension, are resolved for the task only
when a rendered template references them, and a resolver returning ``None``
stops an agent step reading the value before it starts (``value_unavailable``)
and fails an automatic handler reading it.

Branch strategies
-----------------

An extension that names branches from ``start --branch-strategy`` may declare
``branch_strategies``. It receives the extension's settings and returns the
strategy names it accepts, which ``discover`` lists for agents.

Retries
-------

An extension handler is one unit of work. When a run is retried, the whole
handler runs again — there is no per-step resumption inside it, unlike the
command-by-command ledger a shell handler gets. A handler may provide ``check``
to let ww ask whether an interrupted operation already took effect. It receives
the same stable operation ID as ``run`` and returns an
:class:`ExtensionCheckResult` with an explicit succeeded, not-succeeded, or
unknown outcome. Checker errors remain unknown and never become handler
failures. Without a checker, ww leaves the operation interrupted until an
explicit recovery action is chosen.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from ww.extensions.store import ExtensionStore
from ww.validation import NAME_PATTERN as _GROUP_NAME
from ww.validation import is_strict_int
from ww.variables import RESERVED_NAMESPACES, WW_NAMESPACE, is_reserved_name
from ww.workflow_config import ModeDefinition, ProvidedVariable, RuleHints

__all__ = [
    "EXTENSION_API_VERSION",
    "Extension",
    "ExtensionCommand",
    "ExtensionContext",
    "ExtensionHandler",
    "ExtensionNamespace",
    "ExtensionResult",
    "ExtensionCheckResult",
    "ExtensionVariable",
    "ModeDefinition",
    "ProvidedVariable",
    "RuleGroupContribution",
    "RuleHints",
]

EXTENSION_API_VERSION = 1
# A lower-case name segment, e.g. "github" or "pull_request"; "GitHub" does not
# match.
_SEGMENT = re.compile(r"[a-z0-9][a-z0-9_-]*$")
# A value name, dots and hyphens allowed, e.g. "pr.url" or "base-branch".
_VALUE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*$")


@dataclass(frozen=True)
class ExtensionResult:
    """What an extension handler reports back to the executor.

    ``output`` is human-readable diagnostic output. ``values`` is the
    structured, machine-readable part of a successful result; every key must
    have been declared by :attr:`ExtensionHandler.outputs` and ww adds those
    values to the workflow value environment.
    """

    ok: bool
    output: str = ""
    error: str = ""
    working_directory: Path | None = None
    values: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if type(self.ok) is not bool:
            raise TypeError("extension result ok must be a bool")
        if not isinstance(self.output, str) or not isinstance(self.error, str):
            raise TypeError("extension result output and error must be strings")
        if self.working_directory is not None and not isinstance(
            self.working_directory, Path
        ):
            raise TypeError("extension result working_directory must be a Path")
        if not isinstance(self.values, Mapping):
            raise TypeError("extension result values must be a mapping")
        copied = dict(self.values)
        for name, value in copied.items():
            if not isinstance(name, str) or not _VALUE_NAME.fullmatch(name):
                raise TypeError(
                    "extension result value names must be normalized strings"
                )
            if is_reserved_name(name):
                raise ValueError(f"extension result value name {name!r} is reserved")
            if not isinstance(value, str):
                raise TypeError("extension result values must be strings")
        if not self.ok and copied:
            raise ValueError("a failed extension result cannot provide values")
        object.__setattr__(self, "values", MappingProxyType(copied))


@dataclass(frozen=True)
class ExtensionCheckResult:
    """Tri-state attestation of an interrupted extension operation."""

    status: Literal["succeeded", "not_succeeded", "unknown"]
    result: ExtensionResult | None = None
    error: str = ""

    def __post_init__(self) -> None:
        if self.status not in {"succeeded", "not_succeeded", "unknown"}:
            raise ValueError("extension check result has an invalid status")
        if not isinstance(self.error, str):
            raise TypeError("extension check result error must be a string")
        if self.status == "succeeded":
            if not isinstance(self.result, ExtensionResult) or not self.result.ok:
                raise ValueError(
                    "a succeeded extension check requires a successful result"
                )
        elif self.result is not None:
            raise ValueError("only a succeeded extension check may include a result")

    @classmethod
    def succeeded(cls, result: ExtensionResult) -> ExtensionCheckResult:
        return cls("succeeded", result=result)

    @classmethod
    def not_succeeded(cls) -> ExtensionCheckResult:
        return cls("not_succeeded")

    @classmethod
    def unknown(cls, error: str = "") -> ExtensionCheckResult:
        return cls("unknown", error=error)


@dataclass(frozen=True)
class ExtensionContext:
    """Everything a handler or command is allowed to know about the caller.

    ``task_id``, ``run_id`` and ``workflow`` are absent for commands invoked
    outside a task. ``values`` holds the workflow values collected so far, so a
    handler reads its declared inputs from it by name.

    ``config`` is this extension's own section of ``ww.json``,
    and nothing else from that file. ww passes it through unvalidated: it cannot
    know a third party's schema, so an extension validates its own settings and
    reports its own errors.
    """

    root: Path
    store: ExtensionStore
    config: Mapping[str, object] = field(default_factory=dict)
    task_id: str | None = None
    run_id: str | None = None
    workflow: str | None = None
    values: Mapping[str, str] = field(default_factory=dict)
    arguments: tuple[str, ...] = ()
    workspace: Path | None = None
    # Execution identity is absent for standalone extension commands.
    item_id: str | None = None
    work_item_id: str | None = None
    attempt: int = 0
    operation_id: str | None = None


@dataclass(frozen=True)
class ExtensionHandler:
    """Work an extension contributes, addressable as ``.../handlers:<name>``.

    ``provide`` declares inputs ww collects from the agent before running the
    handler, exactly as a configured CLI handler does.  ``validate`` judges
    those inputs when the agent supplies them: it receives the handler's own
    declared values and returns an error message to refuse them, or ``None``.
    ww then rejects the completion carrying a refused value before saving
    anything, so the agent corrects it instead of the handler failing later.

    ``arguments`` names the positional arguments a reference must pass with
    ``args``, in order; ww renders their templates when the handler runs and
    hands them over as ``ExtensionContext.arguments``.  A reference passing
    a different number is a configuration error.
    """

    name: str
    run: Callable[[ExtensionContext], ExtensionResult]
    description: str = ""
    provide: tuple[ProvidedVariable, ...] = ()
    check: Callable[[ExtensionContext], ExtensionCheckResult] | None = None
    outputs: tuple[str, ...] = ()
    validate: Callable[[Mapping[str, str]], str | None] | None = None
    arguments: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _VALUE_NAME.fullmatch(self.name):
            raise ValueError("extension handler name must be normalized")
        if not isinstance(self.description, str):
            raise TypeError("extension handler description must be a string")
        if not callable(self.run):
            raise TypeError("extension handler run must be callable")
        if self.check is not None and not callable(self.check):
            raise TypeError("extension handler check must be callable")
        if self.validate is not None and not callable(self.validate):
            raise TypeError("extension handler validate must be callable")
        if not isinstance(self.provide, tuple) or not all(
            isinstance(value, ProvidedVariable) for value in self.provide
        ):
            raise TypeError("extension handler provide must be a tuple of variables")
        provided_names = [value.name for value in self.provide]
        if len(provided_names) != len(set(provided_names)):
            raise ValueError("extension handler input names must be unique")
        for value in self.provide:
            if (
                not isinstance(value.name, str)
                or not _VALUE_NAME.fullmatch(value.name)
                or is_reserved_name(value.name)
            ):
                raise ValueError("extension handler input names must be normalized")
            if not isinstance(value.description, str):
                raise TypeError("extension handler input descriptions must be strings")
        if not isinstance(self.outputs, tuple):
            raise TypeError("extension handler outputs must be a tuple")
        if len(self.outputs) != len(set(self.outputs)):
            raise ValueError("extension handler output names must be unique")
        for name in self.outputs:
            if not isinstance(name, str) or not _VALUE_NAME.fullmatch(name):
                raise ValueError(
                    "extension handler output names must be normalized strings"
                )
            if is_reserved_name(name):
                raise ValueError(f"extension handler output name {name!r} is reserved")
        if not isinstance(self.arguments, tuple) or not all(
            isinstance(name, str) and _VALUE_NAME.fullmatch(name)
            for name in self.arguments
        ):
            raise ValueError(
                "extension handler arguments must be a tuple of normalized names"
            )
        if len(self.arguments) != len(set(self.arguments)):
            raise ValueError("extension handler argument names must be unique")


@dataclass(frozen=True)
class ExtensionCommand:
    """An operator command over the extension's own state.

    Commands are reachable only as ``./ww extension <vendor>/<name> <command>``.
    They are deliberately not addressable from ``ww.yaml``:
    they inspect and maintain what the extension has recorded, and are
    not workflow steps.
    """

    name: str
    run: Callable[[ExtensionContext], str]
    description: str = ""
    usage: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _VALUE_NAME.fullmatch(self.name):
            raise ValueError("extension command name must be normalized")
        if not callable(self.run):
            raise TypeError("extension command run must be callable")
        if not isinstance(self.description, str) or not isinstance(self.usage, str):
            raise TypeError("extension command description and usage must be strings")


@dataclass(frozen=True)
class ExtensionVariable:
    """A variable an extension resolves for a task.

    In :attr:`Extension.variables` it overrides a variable defined by ww core:
    returning ``None`` keeps the core value, and the core name is validated
    when the override is resolved. In an :class:`ExtensionNamespace` it is a
    new name under the extension's namespace, and ``None`` means the value is
    not available yet.
    """

    name: str
    resolve: Callable[[ExtensionContext], str | None]

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _VALUE_NAME.fullmatch(self.name):
            raise ValueError("extension variable name must be normalized")
        if not callable(self.resolve):
            raise TypeError("extension variable resolver must be callable")


@dataclass(frozen=True)
class ExtensionNamespace:
    """Template values an extension provides as ``{{ww.<name>.<variable>}}``.

    Each variable resolves lazily, for the task on the context, and only when
    a template being rendered references it; returning ``None`` means the
    value is not available for that task yet, which stops the task for the
    operator (``value_unavailable``) before a step reading it starts, or
    fails a handler reading it.  Unlike an :class:`ExtensionVariable`, these
    are new names, available whenever the project configures the extension
    (a section in the settings, even an empty one), not only when a handler
    of the extension is referenced.
    """

    name: str
    variables: tuple[ExtensionVariable, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _SEGMENT.fullmatch(self.name):
            raise ValueError(
                "extension namespace must be lowercase letters, digits, '_' or "
                "'-', starting with a letter or digit"
            )
        if self.name == WW_NAMESPACE or self.name in RESERVED_NAMESPACES:
            raise ValueError(f"extension namespace {self.name!r} is reserved")
        if not isinstance(self.variables, tuple) or not self.variables:
            raise ValueError("extension namespace variables must be a non-empty tuple")
        if not all(isinstance(value, ExtensionVariable) for value in self.variables):
            raise TypeError("extension namespace variables contain an invalid item")
        names = [value.name for value in self.variables]
        if len(names) != len(set(names)):
            raise ValueError("extension namespace variable names must be unique")


@dataclass(frozen=True)
class RuleGroupContribution:
    """A rule group an extension ships, before it is resolved.

    ``items`` are absolute paths to rule files or directories, or the names
    of other groups; an extension builds its paths from its own module file,
    for example ``Path(__file__).parent / "rules"``.
    """

    name: str
    items: tuple[Path | str, ...]
    workflows: tuple[str, ...] | None = None
    steps: tuple[str, ...] | None = None
    hints: RuleHints = RuleHints()

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _GROUP_NAME.fullmatch(self.name):
            raise ValueError("rule group name must be a normalized name")
        if not isinstance(self.items, tuple) or not self.items:
            raise ValueError("rule group items must be a non-empty tuple")
        for item in self.items:
            if isinstance(item, Path):
                if not item.is_absolute():
                    raise ValueError("rule group paths must be absolute")
            elif not isinstance(item, str) or not _GROUP_NAME.fullmatch(item):
                raise ValueError(
                    "rule group items must be absolute paths or group names"
                )
        for label, names in (("workflows", self.workflows), ("steps", self.steps)):
            if names is not None and (
                not isinstance(names, tuple)
                or not all(isinstance(name, str) and name for name in names)
            ):
                raise ValueError(f"rule group {label} must be a tuple of names")
        if not isinstance(self.hints, RuleHints):
            raise TypeError("rule group hints must be RuleHints")


@dataclass(frozen=True)
class Extension:
    """A vendor's contribution of handlers, modes, commands, and overrides."""

    vendor: str
    name: str
    description: str = ""
    handlers: tuple[ExtensionHandler, ...] = ()
    modes: tuple[ModeDefinition, ...] = ()
    commands: tuple[ExtensionCommand, ...] = ()
    variables: tuple[ExtensionVariable, ...] = ()
    version: str = "0"
    api_version: int = EXTENSION_API_VERSION
    # Paths a task claims outside ww state; receives config, root, task_id,
    # and workflow on the context.  ``None`` when the extension owns none.
    reserved_paths: Callable[[ExtensionContext], tuple[Path, ...]] | None = None
    # Whether the extension still holds records for ``task_id`` (same context
    # as ``reserved_paths``), so a generated ID skips it.  ``None`` for never.
    claims_task: Callable[[ExtensionContext], bool] | None = None
    # Forget ``task_id`` and its children when the task is reset; receives
    # root, store, config, and task_id.  ``None`` when there is nothing to drop.
    forget_task: Callable[[ExtensionContext], None] | None = None
    # Names accepted by ``start --branch-strategy``, given the extension's
    # settings.  ``None`` when the extension does not name branches.
    branch_strategies: Callable[[Mapping[str, object]], tuple[str, ...]] | None = None
    # Rule groups the extension ships; a project that configures the
    # extension gets them under the root ``rules`` by these names.
    rules: tuple[RuleGroupContribution, ...] = ()
    # Template values under ``{{ww.<namespace>.*}}``; ``None`` for none.
    namespace: ExtensionNamespace | None = None

    def __post_init__(self) -> None:
        if self.namespace is not None and not isinstance(
            self.namespace, ExtensionNamespace
        ):
            raise TypeError("extension namespace must be an ExtensionNamespace")
        if self.reserved_paths is not None and not callable(self.reserved_paths):
            raise TypeError("extension reserved_paths must be callable")
        if self.claims_task is not None and not callable(self.claims_task):
            raise TypeError("extension claims_task must be callable")
        if self.forget_task is not None and not callable(self.forget_task):
            raise TypeError("extension forget_task must be callable")
        if self.branch_strategies is not None and not callable(self.branch_strategies):
            raise TypeError("extension branch_strategies must be callable")
        for label, value in (("vendor", self.vendor), ("name", self.name)):
            if not _SEGMENT.fullmatch(value):
                raise ValueError(
                    f"extension {label} {value!r} must be lowercase letters, "
                    "digits, '_' or '-', starting with a letter or digit"
                )
        if not isinstance(self.description, str):
            raise TypeError("extension description must be a string")
        if not isinstance(self.version, str) or not self.version.strip():
            raise ValueError("extension version must be a non-empty string")
        if not is_strict_int(self.api_version):
            raise TypeError("extension api_version must be an integer")
        for label, values, expected in (
            ("handlers", self.handlers, ExtensionHandler),
            ("modes", self.modes, ModeDefinition),
            ("commands", self.commands, ExtensionCommand),
            ("variables", self.variables, ExtensionVariable),
            ("rules", self.rules, RuleGroupContribution),
        ):
            if not isinstance(values, tuple):
                raise TypeError(f"extension {label} must be a tuple")
            if not all(isinstance(value, expected) for value in values):
                raise TypeError(f"extension {label} contain an invalid item")
            names = [value.name for value in values]
            if len(names) != len(set(names)):
                raise ValueError(f"extension {label} names must be unique")
        for mode in self.modes:
            if not isinstance(mode.name, str) or not _VALUE_NAME.fullmatch(mode.name):
                raise ValueError("extension mode name must be normalized")
            if not isinstance(mode.description, tuple) or not all(
                isinstance(line, str) for line in mode.description
            ):
                raise TypeError("extension mode description must be a tuple of strings")

    @property
    def identifier(self) -> str:
        return f"{self.vendor}/{self.name}"

    @property
    def handlers_by_name(self) -> dict[str, ExtensionHandler]:
        return {handler.name: handler for handler in self.handlers}

    @property
    def modes_by_name(self) -> dict[str, ModeDefinition]:
        return {mode.name: mode for mode in self.modes}

    @property
    def commands_by_name(self) -> dict[str, ExtensionCommand]:
        return {command.name: command for command in self.commands}
