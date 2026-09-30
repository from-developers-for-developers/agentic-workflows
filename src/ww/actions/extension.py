# SPDX-License-Identifier: GPL-3.0-or-later
"""Extension action planning, identity, and execution effects."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from ww.errors import ConfigurationError
from ww.extensions import (
    ExtensionCheckResult,
    ExtensionResult,
    parse_reference,
)
from ww.validation import (
    expect_keys,
    expect_optional_int,
    expect_optional_mapping,
    expect_optional_string,
    expect_string,
)

from .contracts import (
    ActionResult,
    ActionTraits,
    AutomaticAction,
    ExecutionContext,
    Extension,
    ExtensionBinding,
    InputValidationContext,
    InstructionContent,
    InstructionContext,
    PreflightContext,
    RecoveryCheckResult,
    RecoveryContext,
    ResolutionContext,
)


class ExtensionAction(AutomaticAction[Extension, Extension]):
    identifier = "extension"
    planned_type = Extension

    def preflight(self, planned: Extension, context: PreflightContext) -> None:
        context.extensions.validate_identity(planned)

    def validate_inputs(
        self,
        planned: Extension,
        values: Mapping[str, str],
        context: InputValidationContext,
    ) -> str | None:
        """Let the handler's own ``validate`` judge its declared inputs."""
        handler = context.extensions.handler(planned.reference)
        if handler.validate is None:
            return None
        own = {
            value.name: values[value.name]
            for value in handler.provide
            if value.name in values
        }
        try:
            verdict = handler.validate(MappingProxyType(own))
        except Exception as error:  # noqa: BLE001 - extension exceptions are refusals
            return f"{type(error).__name__}: {error}"
        if verdict is None:
            return None
        if not isinstance(verdict, str) or not verdict.strip():
            return "extension validator returned an invalid result"
        return verdict.strip()

    def execute(self, planned: Extension, context: ExecutionContext) -> ActionResult:
        handler = context.extensions.handler(planned.reference)
        try:
            result = handler.run(context.extensions.context(planned))
        except Exception as error:  # noqa: BLE001 - extension exceptions are action failures
            return ActionResult.failed(f"{type(error).__name__}: {error}")
        if not isinstance(result, ExtensionResult):
            return ActionResult.failed(
                "extension returned an invalid result: expected ExtensionResult"
            )
        if not result.ok:
            return ActionResult.failed(result.error.strip() or "no error reported")
        return ActionResult.succeeded(
            result.output,
            values=result.values,
            working_directory=result.working_directory,
        )

    def check_recovery(
        self, planned: Extension, context: RecoveryContext
    ) -> RecoveryCheckResult:
        if not context.operation_id_known:
            return RecoveryCheckResult.unknown(
                "operation ID was synthesized while migrating legacy state"
            )
        context.extensions.validate_identity(planned)
        try:
            result = context.extensions.check(planned)
            if not isinstance(result, ExtensionCheckResult):
                return RecoveryCheckResult.unknown(
                    "extension checker returned an invalid result"
                )
            if result.status == "succeeded":
                assert result.result is not None  # validated by ExtensionCheckResult
                return RecoveryCheckResult.succeeded(
                    ActionResult.succeeded(
                        result.result.output,
                        values=result.result.values,
                        working_directory=result.result.working_directory,
                    )
                )
            if result.status == "not_succeeded":
                return RecoveryCheckResult.not_succeeded()
            return RecoveryCheckResult.unknown(result.error)
        except Exception as error:  # noqa: BLE001 - checker errors remain unknown
            return RecoveryCheckResult.unknown(f"{type(error).__name__}: {error}")

    def traits(self, planned: Extension) -> ActionTraits:
        return ActionTraits(
            manual_attestation=frozenset({"values", "workspace"}),
            extension_binding=ExtensionBinding(planned.reference, planned.settings),
        )

    def parse(
        self, source: dict[str, Any], name: str, description: str, path: str
    ) -> Extension:
        return Extension(name)

    def validate(self, definition: Extension, path: str) -> None:
        parse_reference(definition.reference)

    def templates(self, definition: Extension) -> tuple[str, ...]:
        return definition.arguments

    def plan(self, definition: Extension, context: ResolutionContext) -> Extension:
        if context.extensions is None:
            raise ConfigurationError("no extensions are loaded")
        reference = parse_reference(definition.reference)
        identity = context.extensions.identity(reference.identifier)
        return Extension(
            definition.reference,
            identity.version,
            identity.api_version,
            identity.source,
            identity.fingerprint,
            context.extensions.settings(reference.identifier, context.project),
            tuple(context.interpolate(argument) for argument in definition.arguments),
        )

    def instruction(
        self, planned: Extension, context: InstructionContext
    ) -> InstructionContent:
        return InstructionContent(context.description or context.name, ())

    def encode(self, planned: Extension) -> dict[str, object]:
        encoded: dict[str, object] = {
            "reference": planned.reference,
            "version": planned.version,
            "api_version": planned.api_version,
            "source": planned.source,
            "fingerprint": planned.fingerprint,
            "settings": planned.settings,
        }
        # Absent without ``args``, so plans saved before arguments existed
        # read and compare unchanged.
        if planned.arguments:
            encoded["arguments"] = list(planned.arguments)
        return encoded

    def decode(self, data: dict[str, Any]) -> Extension:
        expect_keys(
            data,
            {
                "reference",
                "version",
                "api_version",
                "source",
                "fingerprint",
                "settings",
            },
            f"action {self.identifier!r} payload",
        )
        return Extension(
            expect_string(data["reference"], "extension reference"),
            expect_optional_string(data["version"], "extension version"),
            expect_optional_int(data["api_version"], "extension API version"),
            expect_optional_string(data["source"], "extension source"),
            expect_optional_string(data["fingerprint"], "extension fingerprint"),
            expect_optional_mapping(data["settings"], "extension settings"),
            _arguments(data.get("arguments", [])),
        )


def _arguments(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(
        isinstance(argument, str) for argument in value
    ):
        raise ValueError("extension arguments must be a list of strings")
    return tuple(value)
