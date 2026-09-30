# SPDX-License-Identifier: GPL-3.0-or-later
"""Typed action payloads, phase contexts, and the internal registry."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Generic, Literal, Protocol, TypeVar

from ww.contracts import AssertionOperator, ExecutionKind, PlanItemOwner
from ww.discovery import AvailableActions
from ww.errors import ConfigurationError
from ww.extensions import (
    ExtensionCheckResult,
    ExtensionContext,
    ExtensionHandler,
    ExtensionRegistry,
)
from ww.interpolation import dependencies, interpolate
from ww.validation import (
    expect_literal,
    expect_string,
    is_strict_int,
)


@dataclass(frozen=True)
class Prompt:
    text: str


@dataclass(frozen=True)
class Skill:
    name: str


@dataclass(frozen=True)
class SlashCommand:
    name: str


@dataclass(frozen=True)
class Mcp:
    connection: str
    prompt: str


@dataclass(frozen=True)
class AssertionDefinition:
    """What a command's output must be: equal to ``expected``, or empty.

    ``expected`` is ``None`` exactly when the operator is ``empty``.
    """

    operator: AssertionOperator
    expected: str | None = None

    def __post_init__(self) -> None:
        if self.operator not in {"eq", "empty"}:
            raise ValueError(f"invalid assertion operator: {self.operator!r}")
        if (self.operator == "eq") != isinstance(self.expected, str):
            raise ValueError(
                "an eq assertion requires an expected value; empty takes none"
            )

    def holds(self, output: str) -> bool:
        """Whether the command's stripped output satisfies the assertion."""
        if self.operator == "empty":
            return output.strip() == ""
        return output == self.expected

    def describe(self) -> str:
        """The requirement in words, for instructions and failure messages."""
        if self.operator == "empty":
            return "Command output must be empty."
        return f"Command output must equal `{self.expected}`."

    def to_dict(self) -> dict[str, str]:
        if self.expected is None:
            return {"operator": self.operator}
        return {"operator": self.operator, "expected": self.expected}


@dataclass(frozen=True)
class CommandDefinition:
    """A command whose data arguments are distinct from optional shell source."""

    argv: tuple[str, ...] = ()
    shell: str | None = None
    args: tuple[str, ...] = ()
    env: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if bool(self.argv) == (self.shell is not None):
            raise ValueError("command requires exactly one of argv or shell")
        if self.shell is None and (self.args or self.env):
            raise ValueError("command args and env require shell source")

    def to_dict(self) -> dict[str, object]:
        if self.shell is None:
            return {"argv": list(self.argv)}
        result: dict[str, object] = {"shell": self.shell}
        if self.args:
            result["args"] = list(self.args)
        if self.env:
            result["env"] = dict(self.env)
        return result

    @property
    def templates(self) -> tuple[str, ...]:
        """Return interpolated fields; shell source is intentionally excluded."""
        return (*self.argv, *self.args, *(value for _, value in self.env))


@dataclass(frozen=True)
class Commands:
    commands: tuple[CommandDefinition, ...]
    assertion: AssertionDefinition | None = None
    # The author's statement that running the sequence again is harmless, so
    # an interrupted run may be replayed without an operator decision.
    idempotent: bool = False


@dataclass(frozen=True)
class Extension:
    reference: str
    version: str | None = None
    api_version: int | None = None
    source: str | None = None
    fingerprint: str | None = None
    settings: dict[str, object] | None = None


ActionPayload = (
    Prompt
    | Skill
    | SlashCommand
    | Mcp
    | Commands
    | Extension
)


@dataclass(frozen=True)
class PlannedAction:
    """An action's stable identity and typed compiled payload."""

    identifier: str
    payload: object
    serialized_payload: dict[str, object] | None = field(
        default=None, compare=False, repr=False
    )

    @property
    def kind(self) -> str:
        """An ordinary action is identified by its registry identifier."""
        return self.identifier

    def to_dict(self) -> dict[str, object]:
        if not actions.contains(self.identifier):
            payload = self.serialized_payload
            if payload is None and isinstance(self.payload, dict):
                payload = self.payload
            if payload is None:
                raise ValueError("unavailable action payload must remain plain data")
            return {"identifier": self.identifier, "payload": payload}
        return {
            "identifier": self.identifier,
            "payload": actions.get(self.identifier).encode(self.payload),
        }

    @classmethod
    def from_dict(cls, data: object) -> PlannedAction:
        if not isinstance(data, dict) or not {"identifier", "payload"} <= set(data):
            raise ValueError("plan action must have identifier and payload")
        identifier = expect_string(data["identifier"], "action identifier")
        payload = data["payload"]
        if not isinstance(payload, dict):
            raise ValueError("plan action payload must be a mapping")
        if not actions.contains(identifier):
            return cls(identifier, payload)
        return cls(identifier, actions.get(identifier).decode(payload), payload)


@dataclass(frozen=True)
class DefinedAction:
    """A normalized definition before compilation and interpolation."""

    identifier: str
    payload: object


@dataclass(frozen=True)
class DefinitionOverrideContext:
    """Notation-level facts an action may use when a handler is reused.

    Shared handler precedence stays in the configuration layer.  An action may
    own the meaning of omitted and explicit fields in its own payload.
    """

    mapping: Mapping[str, object]
    action_fields: Mapping[str, object]
    description_is_explicit: bool
    local_name: str
    local_description: str


DefinitionT = TypeVar("DefinitionT")
PlannedT = TypeVar("PlannedT")
AttestationField = Literal["values", "workspace"]


@dataclass(frozen=True)
class ResolutionContext:
    agent: str
    available: AvailableActions
    extensions: ExtensionRegistry | None
    builtins: dict[str, str]
    allowed_variables: frozenset[str]
    # The configured project whose extension settings the item follows: the
    # run's project for an item working in the task workspace or the project
    # directory, none for one working in the root.
    project: str | None = None

    def interpolate(self, value: str) -> str:
        unknown = {
            name
            for name in dependencies(value)
            if name not in self.allowed_variables
            and not name.startswith(
                ("metadata.", "project_metadata.", "item.", "field.", "ww.child.")
            )
        }
        if unknown:
            raise ConfigurationError(
                "handler references unavailable variable(s): "
                + ", ".join(sorted(unknown))
            )
        return interpolate(value, self.builtins)


@dataclass(frozen=True)
class InstructionContext:
    description: str
    name: str
    task_values: dict[str, str]


@dataclass(frozen=True)
class InstructionContent:
    text: str
    markdown: tuple[str, ...]
    show_context: bool = True
    after_shared: tuple[str, ...] = ()
    include_item_context: bool = False


@dataclass(frozen=True)
class ExtensionBinding:
    """An extension reference and the frozen settings a payload binds to it."""

    reference: str
    settings: dict[str, object] | None


@dataclass(frozen=True)
class ActionTraits:
    """How core treats one planned payload.

    Every field has the plain default, so an action only names what it needs:
    ``command_segments`` is ``None`` unless the action runs durable command
    segments, ``attests_output`` says an interrupted command needs operator
    stdout to settle its assertion, ``manual_attestation`` lists what an
    operator may attest after an interruption, and ``idempotent`` lets core
    replay an interrupted action without asking anyone.
    """

    attests_output: bool = False
    artifact_attribution: str = "auto"
    manual_attestation: frozenset[AttestationField] = frozenset()
    extension_binding: ExtensionBinding | None = None
    command_segments: tuple[CommandDefinition, ...] | None = None
    idempotent: bool = False


class Action(ABC, Generic[DefinitionT, PlannedT]):
    """One kind of ordinary work an agent performs or ww runs on its behalf.

    A subclass owns its payload types and their whole lifecycle: parsing the
    YAML shape, validating and planning it, rendering the instruction, and
    encoding the planned payload for the saved plan.  The two hooks at the end
    have safe defaults; override them only when the action has that behaviour.
    Workflow structure (loops, handoffs, children) is not an action.
    """

    identifier: ClassVar[str]
    owner: ClassVar[PlanItemOwner] = "agent"
    execution: ClassVar[ExecutionKind] = "agent_instruction"
    planned_type: type[PlannedT]

    @abstractmethod
    def parse(
        self, source: dict[str, Any], name: str, description: str, path: str
    ) -> DefinitionT:
        """Read the action's own fields from an authored handler mapping."""

    @abstractmethod
    def validate(self, definition: DefinitionT, path: str) -> None:
        """Reject a definition that parsed but cannot be planned."""

    @abstractmethod
    def templates(self, definition: DefinitionT) -> tuple[str, ...]:
        """Return the fields core scans for ``{{variable}}`` dependencies."""

    @abstractmethod
    def plan(self, definition: DefinitionT, context: ResolutionContext) -> PlannedT:
        """Resolve a definition against one agent, project, and variable set."""

    @abstractmethod
    def instruction(
        self, planned: PlannedT, context: InstructionContext
    ) -> InstructionContent:
        """Render what the agent (or ``ww plan``) shows for this payload."""

    @abstractmethod
    def encode(self, planned: PlannedT) -> dict[str, object]:
        """Return the plain-data form saved in a plan snapshot."""

    @abstractmethod
    def decode(self, data: dict[str, Any]) -> PlannedT:
        """Rebuild a planned payload strictly from its saved form."""

    def override_definition(
        self,
        local: DefinitionT,
        inherited: DefinitionT,
        context: DefinitionOverrideContext,
    ) -> DefinitionT:
        """Merge a step's local payload with the catalog handler it references.

        Shared handler precedence stays in the configuration layer; an action
        decides what its own omitted fields inherit.  By default the local
        payload replaces the inherited one.
        """
        del inherited, context
        return local

    def traits(self, planned: PlannedT) -> ActionTraits:
        """Describe how core should treat a planned payload."""
        del planned
        return ActionTraits()


@dataclass(frozen=True)
class ActionResult:
    """The complete, validated-by-core outcome proposed by an action."""

    ok: bool
    output: str = ""
    error: str = ""
    values: dict[str, str] = field(default_factory=dict)
    working_directory: Path | None = None

    @classmethod
    def succeeded(
        cls,
        output: str = "",
        *,
        values: Mapping[str, str] | None = None,
        working_directory: Path | None = None,
    ) -> ActionResult:
        return cls(
            True,
            output,
            values=dict(values or {}),
            working_directory=working_directory,
        )

    @classmethod
    def failed(cls, error: str, *, output: str = "") -> ActionResult:
        return cls(False, output, error)


@dataclass(frozen=True)
class RecoveryCheckResult:
    """A checker attestation for an interrupted automatic action.

    ``action`` attests the entire action and therefore permits normal result
    application (output values and working directory).  ``command_segment``
    attests just one durable command boundary; it may carry stdout only and
    the coordinator resumes the remaining action afterwards.  The explicit
    scope prevents a generic success from silently completing a multi-command
    action.
    """

    status: Literal["succeeded", "not_succeeded", "unknown"]
    result: ActionResult | None = None
    scope: Literal["action", "command_segment"] = "action"
    segment: int | None = None
    error: str = ""

    def __post_init__(self) -> None:
        if self.status not in {"succeeded", "not_succeeded", "unknown"}:
            raise ValueError("recovery check result has an invalid status")
        if self.scope not in {"action", "command_segment"}:
            raise ValueError("recovery check result has an invalid scope")
        if not isinstance(self.error, str):
            raise TypeError("recovery check result error must be a string")
        if self.status == "succeeded":
            if not isinstance(self.result, ActionResult) or not self.result.ok:
                raise ValueError(
                    "a succeeded recovery check requires a successful result"
                )
            if self.scope == "command_segment":
                if not is_strict_int(self.segment) or self.segment < 0:
                    raise ValueError(
                        "a command-segment recovery check requires a segment"
                    )
                if self.result.values or self.result.working_directory is not None:
                    raise ValueError(
                        "a command-segment recovery check cannot provide values "
                        "or a working directory"
                    )
            elif self.segment is not None:
                raise ValueError(
                    "an action recovery check cannot identify a command segment"
                )
        elif self.result is not None or self.segment is not None:
            raise ValueError(
                "only a succeeded recovery check may include a result or segment"
            )

    @classmethod
    def succeeded(cls, result: ActionResult) -> RecoveryCheckResult:
        return cls("succeeded", result=result)

    @classmethod
    def succeeded_segment(cls, segment: int, output: str = "") -> RecoveryCheckResult:
        return cls(
            "succeeded",
            result=ActionResult.succeeded(output),
            scope="command_segment",
            segment=segment,
        )

    @classmethod
    def not_succeeded(cls) -> RecoveryCheckResult:
        return cls("not_succeeded")

    @classmethod
    def unknown(cls, error: str = "") -> RecoveryCheckResult:
        return cls("unknown", error=error)


@dataclass(frozen=True)
class CommandRequest:
    """One prepared command segment.  The service owns its durable execution."""

    argv: tuple[str, ...]
    environment: dict[str, str]


@dataclass(frozen=True)
class CommandOutcome:
    """Full command output; previews remain solely in durable state records."""

    ok: bool
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    launch_error: str | None = None


class CommandService(Protocol):
    def completed(self, segment: int) -> CommandOutcome | None: ...

    def execute(self, segment: int, request: CommandRequest) -> CommandOutcome: ...


class ExtensionService(Protocol):
    def validate_identity(self, planned: Extension) -> None: ...

    def context(self, planned: Extension) -> ExtensionContext: ...

    def handler(self, reference: str) -> ExtensionHandler: ...


class ExtensionIdentityService(Protocol):
    """Identity checks available before an external operation is recorded."""

    def validate_identity(self, planned: Extension) -> None: ...


class RecoveryExtensionService(Protocol):
    """Checker-only extension operations available during recovery."""

    def validate_identity(self, planned: Extension) -> None: ...

    def check(self, planned: Extension) -> ExtensionCheckResult: ...


class ExtensionHandlerService(Protocol):
    """Handler lookup only, for checks that run before any work is recorded."""

    def handler(self, reference: str) -> ExtensionHandler: ...


class InputValidationContext(Protocol):
    """What an action may consult while judging values an agent supplied."""

    @property
    def extensions(self) -> ExtensionHandlerService: ...


class ExecutionContext(Protocol):
    """Read-only invocation information plus deliberately narrow effects."""

    @property
    def root(self) -> Path: ...

    @property
    def workspace(self) -> Path | None: ...

    @property
    def runtime_values(self) -> Mapping[str, str]: ...

    @property
    def task_id(self) -> str: ...

    @property
    def run_id(self) -> str | None: ...

    @property
    def operation_id(self) -> str: ...

    @property
    def operation_id_known(self) -> bool: ...

    @property
    def attempt(self) -> int: ...

    @property
    def commands(self) -> CommandService: ...

    @property
    def extensions(self) -> ExtensionService: ...


class PreflightContext(Protocol):
    """Read-only validation context with no effect-capable services."""

    @property
    def root(self) -> Path: ...
    @property
    def workspace(self) -> Path | None: ...
    @property
    def runtime_values(self) -> Mapping[str, str]: ...
    @property
    def task_id(self) -> str: ...
    @property
    def run_id(self) -> str | None: ...
    @property
    def extensions(self) -> ExtensionIdentityService: ...


class RecoveryContext(Protocol):
    """Saved-operation information without command execution capabilities."""

    @property
    def root(self) -> Path: ...
    @property
    def workspace(self) -> Path | None: ...
    @property
    def runtime_values(self) -> Mapping[str, str]: ...
    @property
    def task_id(self) -> str: ...
    @property
    def run_id(self) -> str | None: ...
    @property
    def operation_id(self) -> str: ...
    @property
    def operation_id_known(self) -> bool: ...
    @property
    def attempt(self) -> int: ...
    @property
    def extensions(self) -> RecoveryExtensionService: ...


class AutomaticAction(Action[DefinitionT, PlannedT]):
    """An action ww runs itself, recording its external effects durably.

    Core writes an ``in_progress`` record before calling :meth:`execute`, so an
    interruption leaves an unknown-outcome boundary.  :meth:`preflight` runs
    before that record exists and must have no external effect;
    :meth:`check_recovery` may later settle the outcome without replaying it.
    """

    owner: ClassVar[PlanItemOwner] = "ww"
    execution: ClassVar[ExecutionKind] = "automatic"

    @abstractmethod
    def execute(self, planned: PlannedT, context: ExecutionContext) -> ActionResult:
        """Perform the work through the narrow services on ``context``."""

    def preflight(self, planned: PlannedT, context: PreflightContext) -> None:
        """Validate identity and configuration before core records a start."""
        del planned, context

    def validate_inputs(
        self,
        planned: PlannedT,
        values: Mapping[str, str],
        context: InputValidationContext,
    ) -> str | None:
        """Judge the declared inputs an agent is about to hand this action.

        Core calls this while the completion that carries the values is still
        refusable, so a value the action would reject at run time is turned
        back with this message before anything is saved.  ``values`` holds
        only the action's own declared inputs.  ``None`` accepts them.
        """
        del planned, values, context
        return None

    def check_recovery(
        self, planned: PlannedT, context: RecoveryContext
    ) -> RecoveryCheckResult | None:
        """Settle an interrupted operation's outcome; ``None`` means no checker."""
        del planned, context
        return None


class ActionRegistry:
    """Explicit, replaceable internal registration boundary."""

    def __init__(self) -> None:
        self._actions: dict[str, Action[Any, Any]] = {}

    def register(self, action: Action[Any, Any]) -> None:
        self._validate(action)
        if action.identifier in self._actions:
            raise ValueError(f"action {action.identifier!r} is already registered")
        self._actions[action.identifier] = action

    @staticmethod
    def _validate(action: Action[Any, Any]) -> None:
        """Reject inconsistent registrations at the only extension boundary."""
        if not isinstance(action, Action):
            raise TypeError("actions must subclass ww.actions.Action")
        identifier = getattr(action, "identifier", None)
        if not isinstance(identifier, str) or not identifier:
            raise TypeError("action identifier must be a non-empty string")
        expect_literal(action.owner, PlanItemOwner, f"action {identifier!r} owner")
        expect_literal(
            action.execution, ExecutionKind, f"action {identifier!r} execution"
        )
        if action.owner == "agent" and action.execution != "agent_instruction":
            raise ValueError(
                f"action {identifier!r} has incompatible owner/execution "
                f"combination {action.owner!r}/{action.execution!r}"
            )
        if (action.execution == "automatic") != isinstance(action, AutomaticAction):
            raise TypeError(
                f"action {identifier!r} with execution {action.execution!r} must "
                + ("" if action.execution == "automatic" else "not ")
                + "subclass AutomaticAction"
            )
        if not isinstance(getattr(action, "planned_type", None), type):
            raise TypeError(f"action {identifier!r} planned_type must be a type")

    def get(self, identifier: str) -> Action[Any, Any]:
        try:
            return self._actions[identifier]
        except KeyError as error:
            raise ConfigurationError(
                f"action implementation {identifier!r} is unavailable"
            ) from error

    def contains(self, identifier: str) -> bool:
        return identifier in self._actions

    def unregister(self, identifier: str) -> None:
        """Remove an internal registration, primarily for isolated tests."""
        self._actions.pop(identifier)


actions = ActionRegistry()
