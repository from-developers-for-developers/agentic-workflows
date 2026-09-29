# SPDX-License-Identifier: GPL-3.0-or-later
"""Command action parsing, planning, rendering, and execution effects."""

from __future__ import annotations

import re
import shlex
from collections.abc import Mapping
from typing import Any

from ww.errors import ConfigurationError, StateError
from ww.interpolation import dependencies, interpolate
from ww.validation import (
    expect_bool,
    expect_mapping,
    expect_nonempty_string,
    expect_normalized_name,
    expect_string,
    reject_unknown_keys,
)

from .contracts import (
    ActionResult,
    ActionTraits,
    AssertionDefinition,
    AutomaticAction,
    CommandDefinition,
    CommandOutcome,
    CommandRequest,
    Commands,
    ExecutionContext,
    InstructionContent,
    InstructionContext,
    ResolutionContext,
)

_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*$")


def _failure_message(
    rendered: tuple[str, ...], index: int, outcome: CommandOutcome
) -> str:
    """Say what failed and show what it printed, on whichever stream it used.

    Only stderr used to be reported. Test runners, linters, and type checkers
    overwhelmingly print their diagnostics to stdout, so the commonest failure
    in ww produced "automatic handler failed (1):" and nothing else -- leaving
    the operator, who has to choose between retrying and forcing past it,
    with no basis for the decision.
    """
    detail = outcome.stderr.strip() or outcome.stdout.strip()
    command = shlex.join(rendered) if rendered else ""
    heading = f"automatic handler failed ({outcome.exit_code})"
    if command:
        heading += f" running: {command}"
    elif index:
        heading += f" at command {index + 1}"
    if not detail:
        return f"{heading}; it printed nothing. See the command output artifact."
    return f"{heading}\n\n{_tail(detail)}"


def _tail(detail: str, limit: int = 40) -> str:
    """Keep the end of the output, where a failure normally explains itself."""
    lines = detail.splitlines()
    if len(lines) <= limit:
        return detail
    dropped = len(lines) - limit
    return "\n".join(
        [f"[{dropped} earlier line(s) omitted; the full output is an artifact]"]
        + lines[-limit:]
    )


class CommandAction(AutomaticAction[Commands, Commands]):
    identifier = "cli"
    planned_type = Commands

    def execute(self, planned: Commands, context: ExecutionContext) -> ActionResult:
        """Run the command sequence while the command service records each effect."""
        outputs: list[str] = []
        for index, command in enumerate(planned.commands):
            # Reading completed output first is intentional: inputs may have
            # changed after interruption, but a completed external operation is
            # never re-rendered or replayed merely to aggregate its output.
            outcome = context.commands.completed(index)
            rendered_for_report: tuple[str, ...] = ()
            if outcome is None:
                try:
                    rendered, environment = self.render_command(
                        command, context.runtime_values
                    )
                except StateError as error:
                    return ActionResult.failed(str(error))
                rendered_for_report = tuple(rendered)
                outcome = context.commands.execute(
                    index, CommandRequest(tuple(rendered), environment)
                )
            if not outcome.ok:
                if outcome.launch_error is not None:
                    return ActionResult.failed(
                        f"automatic handler could not launch command {index + 1}: "
                        f"{outcome.launch_error}"
                    )
                return ActionResult.failed(
                    _failure_message(rendered_for_report, index, outcome)
                )
            outputs.append(outcome.stdout)
        output = "\n".join(part for part in outputs if part).strip()
        if planned.assertion and not planned.assertion.holds(output):
            expected = (
                "no output"
                if planned.assertion.expected is None
                else repr(planned.assertion.expected)
            )
            return ActionResult.failed(
                f"automatic handler assertion failed: expected {expected}, "
                f"got {output!r}",
                output=output,
            )
        return ActionResult.succeeded(output)

    def traits(self, planned: Commands) -> ActionTraits:
        return ActionTraits(
            attests_output=planned.assertion is not None,
            command_segments=planned.commands,
            idempotent=planned.idempotent,
        )

    def parse(
        self, source: dict[str, Any], name: str, description: str, path: str
    ) -> Commands:
        commands, assertion = _parse_command(source, path)
        if not commands:
            raise ConfigurationError(f"{path} requires argv or shell")
        return Commands(commands, assertion, _parse_idempotent(source, path))

    def validate(self, definition: Commands, path: str) -> None:
        if not definition.commands:
            raise ConfigurationError(f"{path} command requires at least one command")

    def templates(self, definition: Commands) -> tuple[str, ...]:
        return tuple(
            template
            for command in definition.commands
            for template in command.templates
        )

    def render_command(
        self, command: CommandDefinition, values: Mapping[str, str]
    ) -> tuple[list[str], dict[str, str]]:
        missing = {
            name
            for template in command.templates
            for name in dependencies(template)
            if name not in values
        }
        if missing:
            raise StateError(
                "automatic handler is missing variable(s): "
                + ", ".join(sorted(missing))
            )
        environment = {
            name: interpolate(template, values) for name, template in command.env
        }
        if command.shell is not None:
            if dependencies(command.shell):
                raise StateError(
                    "saved shell source contains interpolation; use shell args or env"
                )
            return [
                "/bin/sh",
                "-c",
                command.shell,
                "ww-command",
                *(interpolate(argument, values) for argument in command.args),
            ], environment
        return [interpolate(argument, values) for argument in command.argv], environment

    def plan(self, definition: Commands, context: ResolutionContext) -> Commands:
        commands = tuple(
            CommandDefinition(
                argv=tuple(context.interpolate(value) for value in command.argv),
                shell=command.shell,
                args=tuple(context.interpolate(value) for value in command.args),
                env=tuple(
                    (key, context.interpolate(value)) for key, value in command.env
                ),
            )
            for command in definition.commands
        )
        return Commands(commands, definition.assertion, definition.idempotent)

    def instruction(
        self, planned: Commands, context: InstructionContext
    ) -> InstructionContent:
        return InstructionContent(
            context.description or context.name,
            (
                "> **Automated by ww:** shown for information only; do not run "
                "this command yourself.",
                "",
                "**Run**",
                "",
                "```sh",
                *(_display_command(command) for command in planned.commands),
                "```",
                "",
                *(
                    (
                        "**Recovery**",
                        "",
                        "Idempotent: after an interruption ww replays it "
                        "without asking the operator.",
                        "",
                    )
                    if planned.idempotent
                    else ()
                ),
            ),
            after_shared=(
                (
                    "**Check**",
                    "",
                    planned.assertion.describe(),
                    "",
                )
                if planned.assertion
                else ()
            ),
        )

    def encode(self, planned: Commands) -> dict[str, object]:
        data: dict[str, object] = {
            "commands": [command.to_dict() for command in planned.commands],
            "assert": planned.assertion.to_dict() if planned.assertion else None,
        }
        # The default is left out so plans saved before the key existed still
        # decode, and a plan that relies on it is refused by a ww without it.
        if planned.idempotent:
            data["idempotent"] = True
        return data

    def decode(self, data: dict[str, Any]) -> Commands:
        if not {"commands", "assert"} <= set(data):
            raise ValueError(f"action {self.identifier!r} payload has invalid fields")
        idempotent = data.get("idempotent", False)
        if not isinstance(idempotent, bool):
            raise ValueError(f"action {self.identifier!r} idempotent must be a boolean")
        return Commands(
            _commands_from_list(data["commands"], "action"),
            _assertion_from_dict(data["assert"], "action"),
            idempotent,
        )


def _command_from_dict(data: Any) -> CommandDefinition:
    if not isinstance(data, dict):
        raise ValueError("plan command must be a mapping")
    if "argv" in data:
        if "shell" in data:
            raise ValueError("plan command cannot be both argv and shell")
        argv = data["argv"]
        if (
            not isinstance(argv, list)
            or not argv
            or not all(isinstance(item, str) for item in argv)
        ):
            raise ValueError("plan command argv must be a list of strings")
        return CommandDefinition(argv=tuple(argv))
    script = data.get("shell")
    args = data.get("args", [])
    env = data.get("env", {})
    if (
        not isinstance(script, str)
        or not isinstance(args, list)
        or not all(isinstance(item, str) for item in args)
        or not isinstance(env, dict)
        or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in env.items()
        )
    ):
        raise ValueError("invalid plan shell command")
    return CommandDefinition(shell=script, args=tuple(args), env=tuple(env.items()))


def _assertion_from_dict(value: Any, item_path: str) -> AssertionDefinition | None:
    if value is not None and not isinstance(value, dict):
        raise ValueError(f"{item_path}.assert must be an object or null")
    if value is None:
        return None
    if value.get("operator") == "empty":
        if set(value) != {"operator"}:
            raise ValueError(f"{item_path}.assert has invalid fields")
        return AssertionDefinition(operator="empty")
    if not {"operator", "expected"} <= set(value):
        raise ValueError(f"{item_path}.assert has invalid fields")
    if value["operator"] != "eq":
        raise ValueError("invalid assertion operator")
    return AssertionDefinition(
        operator="eq",
        expected=expect_string(value["expected"], "assertion expected value"),
    )


def _commands_from_list(value: Any, item_path: str) -> tuple[CommandDefinition, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{item_path}.commands must be a list")
    result = []
    for index, command in enumerate(value):
        try:
            result.append(_command_from_dict(command))
        except ValueError as error:
            raise ValueError(f"{item_path}.commands[{index}]: {error}") from error
    return tuple(result)


def _parse_assertion(raw: object, path: str) -> AssertionDefinition:
    context = f"{path}.assert"
    assertion = expect_mapping(raw, context, error=ConfigurationError)
    reject_unknown_keys(
        assertion, {"operator", "expected"}, context, error=ConfigurationError
    )
    operator = expect_normalized_name(
        assertion.get("operator"), f"{context}.operator", error=ConfigurationError
    )
    if operator == "empty":
        if "expected" in assertion:
            raise ConfigurationError(
                f"{context}.expected is not allowed with operator empty"
            )
        return AssertionDefinition("empty")
    if operator != "eq":
        raise ConfigurationError(f"{context}.operator must be eq or empty")
    expected = expect_nonempty_string(
        assertion.get("expected"), f"{context}.expected", error=ConfigurationError
    )
    return AssertionDefinition("eq", expected)


def _parse_command(
    mapping: dict[str, Any], path: str
) -> tuple[tuple[CommandDefinition, ...], AssertionDefinition | None]:
    if "command" in mapping:
        if {"argv", "shell", "args", "env", "assert"} & set(mapping):
            raise ConfigurationError(
                f"{path} cannot combine command with root command fields"
            )
        value = mapping["command"]
        assertion = None
        if isinstance(value, dict) and not ({"argv", "shell"} & set(value)):
            reject_unknown_keys(
                value, {"command", "assert"}, path, error=ConfigurationError
            )
            if "command" not in value:
                raise ConfigurationError(f"{path} command mapping requires command")
            raw_commands = value["command"]
            if value.get("assert") is not None:
                assertion = _parse_assertion(value["assert"], path)
        else:
            raw_commands = value
        values = raw_commands if isinstance(raw_commands, list) else [raw_commands]
        if not values or not all(isinstance(item, dict) for item in values):
            raise ConfigurationError(
                f"{path} command must contain action mappings with argv or shell"
            )
        return tuple(_parse_command_action(item, path) for item in values), assertion
    action_keys = {"argv", "shell"} & set(mapping)
    if not action_keys:
        if {"args", "env", "assert", "idempotent"} & set(mapping):
            raise ConfigurationError(
                f"{path} args, env, assert, and idempotent require argv or shell"
            )
        return (), None
    if len(action_keys) != 1:
        raise ConfigurationError(f"{path} cannot combine argv and shell")
    action = {
        key: mapping[key] for key in action_keys | ({"args", "env"} & set(mapping))
    }
    assertion = (
        _parse_assertion(mapping["assert"], path)
        if mapping.get("assert") is not None
        else None
    )
    return (_parse_command_action(action, path),), assertion


def _parse_idempotent(mapping: Mapping[str, Any], path: str) -> bool:
    """Read the handler-level replay declaration; it defaults to ``False``."""
    if "idempotent" not in mapping:
        return False
    return expect_bool(
        mapping["idempotent"], f"{path}.idempotent", error=ConfigurationError
    )


def _parse_command_action(value: dict[Any, Any], path: str) -> CommandDefinition:
    if "argv" in value:
        reject_unknown_keys(value, {"argv"}, path, error=ConfigurationError)
        argv = value["argv"]
        if (
            not isinstance(argv, list)
            or not argv
            or not all(isinstance(item, str) and item for item in argv)
        ):
            raise ConfigurationError(f"{path} argv must be a non-empty list of strings")
        return CommandDefinition(argv=tuple(argv))
    if "shell" in value:
        reject_unknown_keys(
            value, {"shell", "args", "env"}, path, error=ConfigurationError
        )
        script = value["shell"]
        if not isinstance(script, str) or not script.strip():
            raise ConfigurationError(f"{path} shell must be a non-empty string")
        if dependencies(script):
            raise ConfigurationError(
                f"{path} shell source cannot interpolate values; use args or env"
            )
        args = value.get("args", [])
        if not isinstance(args, list) or not all(
            isinstance(item, str) for item in args
        ):
            raise ConfigurationError(f"{path} shell args must be a list of strings")
        raw_env = value.get("env", {})
        if not isinstance(raw_env, dict) or not all(
            isinstance(name, str)
            and _ENV_NAME.fullmatch(name)
            and isinstance(item, str)
            for name, item in raw_env.items()
        ):
            raise ConfigurationError(
                f"{path} shell env must map variable names to strings"
            )
        return CommandDefinition(
            shell=script,
            args=tuple(args),
            env=tuple(raw_env.items()),
        )
    raise ConfigurationError(f"{path} command action requires argv or shell")


def _display_command(command: CommandDefinition) -> str:
    if command.shell is not None:
        environment = " ".join(
            f"{name}={shlex.quote(value)}" for name, value in command.env
        )
        invocation = shlex.join(
            ["/bin/sh", "-c", command.shell, "ww-command", *command.args]
        )
        return " ".join(part for part in (environment, invocation) if part)
    return shlex.join(command.argv)
