# SPDX-License-Identifier: GPL-3.0-or-later
"""Behaviour of each built-in action through its public lifecycle methods."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest

from ww.actions import (
    ActionTraits,
    AssertionCondition,
    AssertionDefinition,
    CommandDefinition,
    Commands,
    Extension,
    InstructionContext,
    Mcp,
    Prompt,
    ResolutionContext,
    Skill,
    SlashCommand,
    actions,
)
from ww.actions.command import CommandAction
from ww.actions.contracts import CommandOutcome, CommandRequest
from ww.discovery import AvailableActions
from ww.errors import ConfigurationError, StateError
from ww.extensions import ExtensionCheckResult, ExtensionHandler, ExtensionResult
from ww.workflow_config import ProvidedVariable


def _resolution(
    *,
    skills: frozenset[str] = frozenset(),
    commands: frozenset[str] = frozenset(),
    variables: frozenset[str] = frozenset({"task_id"}),
) -> ResolutionContext:
    return ResolutionContext(
        agent="codex",
        available=AvailableActions(skills, commands),
        extensions=None,
        builtins={"task_id": "TASK-1"},
        allowed_variables=variables,
    )


def _context(
    description: str = "", task_values: dict[str, str] | None = None
) -> InstructionContext:
    return InstructionContext(description, "step", task_values or {})


def _round_trip(identifier: str, planned: object) -> object:
    action = actions.get(identifier)
    return action.decode(action.encode(planned))


# --- prompt -----------------------------------------------------------------


def test_prompt_uses_description_then_name() -> None:
    action = actions.get("prompt")

    assert action.parse({}, "step", "Do the work.", "p") == Prompt("Do the work.")
    assert action.parse({}, "step", "", "p") == Prompt("step")
    with pytest.raises(ConfigurationError, match="p prompt requires text"):
        action.validate(Prompt(""), "p")


def test_prompt_plans_builtins_and_rejects_unknown_variables() -> None:
    action = actions.get("prompt")

    assert action.plan(Prompt("Fix {{task_id}}."), _resolution()) == Prompt(
        "Fix TASK-1."
    )
    with pytest.raises(ConfigurationError, match="unavailable variable\\(s\\): x"):
        action.plan(Prompt("{{x}}"), _resolution())


def test_prompt_instruction_interpolates_task_values_and_shows_item_context() -> None:
    action = actions.get("prompt")

    content = action.instruction(
        Prompt("Review {{branch}}."), _context("Review", {"branch": "main"})
    )

    assert content.text == "Review main."
    assert content.markdown == ("**Prompt**", "", "Review main.", "")
    assert content.show_context
    same = action.instruction(Prompt("Review"), _context("Review"))
    assert not same.show_context


# --- skill and slash command --------------------------------------------------


@pytest.mark.parametrize(
    ("identifier", "payload_type", "key", "label", "attribute"),
    [
        ("skill", Skill, "skill", "skill", "skills"),
        ("slash_command", SlashCommand, "slash_command", "slash command", "commands"),
    ],
)
def test_named_actions_require_a_discovered_name(
    identifier: str, payload_type: type, key: str, label: str, attribute: str
) -> None:
    action = actions.get(identifier)

    # ``kind:`` chose the action; the payload is the handler's name.
    assert action.parse({}, "review", "", "p") == payload_type("review")
    with pytest.raises(ConfigurationError, match=f"p {label} requires a name"):
        action.validate(payload_type(""), "p")
    assert action.templates(payload_type("review")) == ()

    available = _resolution(**{attribute: frozenset({"review"})})
    assert action.plan(payload_type("review"), available) == payload_type("review")
    with pytest.raises(
        ConfigurationError, match=f"configured {label} not found for codex: review"
    ):
        action.plan(payload_type("review"), _resolution())


def test_skill_instruction_and_artifact_attribution() -> None:
    action = actions.get("skill")

    content = action.instruction(Skill("review"), _context("Check the diff."))

    assert content.text == "Use the `review` skill. Check the diff."
    assert content.markdown == ("**Use skill**", "", "`review`", "")
    assert action.instruction(Skill("review"), _context()).text == (
        "Use the `review` skill."
    )
    assert action.traits(Skill("review")) == ActionTraits(artifact_attribution="review")


def test_slash_command_instruction() -> None:
    action = actions.get("slash_command")

    content = action.instruction(SlashCommand("ship"), _context("Now."))

    assert content.text == "Run the `/ship` slash command. Now."
    assert content.markdown == ("**Run slash command**", "", "`/ship`", "")
    assert action.traits(SlashCommand("ship")) == ActionTraits()


# --- MCP ------------------------------------------------------------------------


def test_mcp_requires_a_connection_and_prompt() -> None:
    action = actions.get("mcp")

    assert action.parse({"mcp": "jira"}, "create", "Create it.", "p") == Mcp(
        "jira", "Create it."
    )
    assert action.parse({"mcp": "jira"}, "create", "", "p") == Mcp("jira", "create")
    for value in ("", "  ", 3, None):
        with pytest.raises(ConfigurationError, match="p mcp must be non-empty"):
            action.parse({"mcp": value}, "create", "", "p")
    with pytest.raises(ConfigurationError, match="requires a connection and prompt"):
        action.validate(Mcp("jira", ""), "p")


def test_mcp_plans_both_fields_and_renders_failure_guidance() -> None:
    action = actions.get("mcp")

    planned = action.plan(
        Mcp("jira-{{task_id}}", "Open {{task_id}}."),
        _resolution(),
    )
    assert planned == Mcp("jira-TASK-1", "Open TASK-1.")
    assert action.templates(Mcp("a", "b")) == ("a", "b")

    content = action.instruction(
        Mcp("jira", "Open {{key}}."), _context(task_values={"key": "P-1"})
    )
    assert content.text.startswith(
        "For the following work use `jira` mcp connection:\nOpen P-1."
    )
    assert "Use the `fail` command to register that" in content.text


# --- round trips and strict decoding ---------------------------------------------


@pytest.mark.parametrize(
    ("identifier", "planned"),
    [
        ("prompt", Prompt("Do it.")),
        ("skill", Skill("review")),
        ("slash_command", SlashCommand("ship")),
        ("mcp", Mcp("jira", "Open it.")),
        (
            "cli",
            Commands(
                (
                    CommandDefinition(argv=("git", "status")),
                    CommandDefinition(
                        shell='echo "$1"', args=("x",), env=(("A", "b"),)
                    ),
                ),
                AssertionDefinition((AssertionCondition("equals", "x"),)),
            ),
        ),
        ("cli", Commands((CommandDefinition(argv=("true",)),))),
        ("cli", Commands((CommandDefinition(argv=("true",)),), idempotent=True)),
    ],
)
def test_payloads_round_trip(identifier: str, planned: object) -> None:
    assert _round_trip(identifier, planned) == planned


def test_command_idempotent_default_is_omitted_from_saved_plans() -> None:
    action = actions.get("cli")
    plain = Commands((CommandDefinition(argv=("true",)),))

    assert "idempotent" not in action.encode(plain)
    assert action.encode(replace(plain, idempotent=True))["idempotent"] is True
    # A plan saved before the key existed still decodes to the default.
    assert not action.decode({"commands": [{"argv": ["a"]}], "assert": None}).idempotent


@pytest.mark.parametrize(
    ("identifier", "data"),
    [
        ("prompt", {}),
        ("prompt", {"text": 3}),
        ("skill", {"name": None}),
        ("slash_command", {"title": "x"}),
        ("mcp", {"connection": "jira"}),
        ("mcp", {"connection": "jira", "prompt": ["x"]}),
        ("cli", {"commands": []}),
        ("cli", {"commands": "true", "assert": None}),
        ("cli", {"commands": [["true"]], "assert": None}),
        ("cli", {"commands": [{"argv": []}], "assert": None}),
        ("cli", {"commands": [{"argv": ["a"], "shell": "b"}], "assert": None}),
        ("cli", {"commands": [{"shell": "a", "env": {"A": 1}}], "assert": None}),
        ("cli", {"commands": [{"argv": ["a"]}], "assert": "x"}),
        ("cli", {"commands": [{"argv": ["a"]}], "assert": {"operator": "empty"}}),
        ("cli", {"commands": [{"argv": ["a"]}], "assert": []}),
        ("cli", {"commands": [{"argv": ["a"]}], "assert": [{"ne": "x"}]}),
        ("cli", {"commands": [{"argv": ["a"]}], "assert": ["equals"]}),
        (
            "cli",
            {"commands": [{"argv": ["a"]}], "assert": None, "idempotent": "yes"},
        ),
        ("cli", {"commands": [{"argv": ["a"]}], "idempotent": True}),
    ],
)
def test_decode_rejects_malformed_payloads(
    identifier: str, data: dict[str, Any]
) -> None:
    with pytest.raises((ValueError, TypeError)):
        actions.get(identifier).decode(data)


@pytest.mark.parametrize(
    ("kind", "data"),
    [
        ("prompt", {"text": "a", "extra": 1}),
        ("cli", {"commands": [{"shell": "a", "cwd": "/"}], "assert": None}),
        ("cli", {"commands": [{"argv": ["a"], "extra": True}], "assert": None}),
    ],
)
def test_decode_leaves_fields_it_does_not_know_alone(kind: str, data: dict) -> None:
    # A stored plan may carry fields another ww version wrote.
    actions.get(kind).decode(data)


# --- command parsing ----------------------------------------------------------


def _parse_command(source: dict[str, Any]) -> Commands:
    return actions.get("cli").parse(source, "run", "", "p")


def test_command_parses_argv_shell_and_nested_forms() -> None:
    assert _parse_command({"argv": ["git", "status"]}) == Commands(
        (CommandDefinition(argv=("git", "status")),)
    )
    assert _parse_command(
        {
            "shell": 'echo "$1" "$NAME"',
            "args": ["{{task_id}}"],
            "env": {"NAME": "x"},
            "assert": [{"equals": "ok"}, "empty"],
        }
    ) == Commands(
        (
            CommandDefinition(
                shell='echo "$1" "$NAME"', args=("{{task_id}}",), env=(("NAME", "x"),)
            ),
        ),
        AssertionDefinition(
            (AssertionCondition("equals", "ok"), AssertionCondition("empty"))
        ),
    )


def test_command_idempotent_is_parsed_planned_and_shown() -> None:
    action = actions.get("cli")
    planned = _parse_command({"argv": ["pytest", "-q"], "idempotent": True})
    plain = _parse_command({"argv": ["pytest", "-q"]})

    assert planned.idempotent
    assert not plain.idempotent
    assert action.plan(planned, _resolution()).idempotent
    assert action.traits(planned).idempotent
    assert not action.traits(plain).idempotent
    shown = "\n".join(action.instruction(planned, _context("Test.")).markdown)
    assert "**Recovery**" in shown
    assert "Idempotent: after an interruption ww replays it" in shown
    assert "Recovery" not in "\n".join(action.instruction(plain, _context()).markdown)


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ({}, "p requires argv or shell"),
        ({"args": ["x"]}, "args, env, assert, and idempotent require argv or shell"),
        ({"idempotent": True}, "idempotent require argv or shell"),
        ({"argv": ["a"], "idempotent": "yes"}, "p.idempotent must be a boolean"),
        ({"argv": ["a"], "shell": "b"}, "cannot combine argv and shell"),
        ({"argv": []}, "argv must be a non-empty list of strings"),
        ({"argv": ["a", ""]}, "argv must be a non-empty list of strings"),
        ({"shell": " "}, "shell must be a non-empty string"),
        ({"shell": "echo {{task_id}}"}, "shell source cannot interpolate values"),
        ({"shell": "a", "args": "x"}, "shell args must be a list of strings"),
        ({"shell": "a", "env": {"1BAD": "x"}}, "shell env must map variable names"),
        ({"shell": "a", "assert": []}, "must be a non-empty list of conditions"),
        ({"shell": "a", "assert": "empty"}, "must be a non-empty list of conditions"),
        ({"shell": "a", "assert": [{"ne": "x"}]}, "unknown"),
        ({"shell": "a", "assert": [{}]}, "must be empty or"),
    ],
)
def test_command_parse_errors(source: dict[str, Any], message: str) -> None:
    with pytest.raises(ConfigurationError, match=message):
        _parse_command(source)


def test_command_plan_interpolates_data_but_never_shell_source() -> None:
    action = actions.get("cli")
    definition = Commands(
        (
            CommandDefinition(argv=("echo", "{{task_id}}")),
            CommandDefinition(
                shell='echo "$1"', args=("{{task_id}}",), env=(("ID", "{{task_id}}"),)
            ),
        )
    )

    planned = action.plan(definition, _resolution())

    assert planned.commands[0].argv == ("echo", "TASK-1")
    assert planned.commands[1].shell == 'echo "$1"'
    assert planned.commands[1].args == ("TASK-1",)
    assert planned.commands[1].env == (("ID", "TASK-1"),)
    assert action.templates(definition) == (
        "echo",
        "{{task_id}}",
        "{{task_id}}",
        "{{task_id}}",
    )
    with pytest.raises(ConfigurationError, match="p command requires at least one"):
        action.validate(Commands(()), "p")


def test_command_instruction_shows_the_commands_and_check() -> None:
    action = actions.get("cli")
    planned = Commands(
        (
            CommandDefinition(argv=("git", "commit", "-m", "a b")),
            CommandDefinition(shell='echo "$1"', args=("x",), env=(("A", "b c"),)),
        ),
        AssertionDefinition((AssertionCondition("equals", "x"),)),
    )

    content = action.instruction(planned, _context("Commit."))

    assert content.text == "Commit."
    assert "git commit -m 'a b'" in content.markdown
    assert "A='b c' /bin/sh -c 'echo \"$1\"' ww-command x" in content.markdown
    assert content.after_shared == (
        "**Check**",
        "",
        "Command output must be equal to `x`.",
        "",
    )
    assert action.traits(planned) == ActionTraits(
        attests_output=True, command_segments=planned.commands
    )
    unchecked = Commands((CommandDefinition(argv=("true",)),))
    assert action.instruction(unchecked, _context()).after_shared == ()
    assert not action.traits(unchecked).attests_output


def test_command_render_substitutes_runtime_values() -> None:
    action = CommandAction()

    argv, environment = action.render_command(
        CommandDefinition(shell='echo "$1"', args=("{{x}}",), env=(("A", "{{x}}"),)),
        {"x": "1"},
    )

    assert argv == ["/bin/sh", "-c", 'echo "$1"', "ww-command", "1"]
    assert environment == {"A": "1"}
    with pytest.raises(StateError, match="missing variable\\(s\\): x"):
        action.render_command(CommandDefinition(argv=("{{x}}",)), {})
    with pytest.raises(StateError, match="saved shell source contains interpolation"):
        action.render_command(CommandDefinition(shell="echo {{x}}"), {"x": "1"})


# --- command execution -----------------------------------------------------------


@dataclass
class _Commands:
    outcomes: list[CommandOutcome]
    completed_outcomes: dict[int, CommandOutcome] = field(default_factory=dict)
    requests: list[tuple[int, CommandRequest]] = field(default_factory=list)

    def completed(self, segment: int) -> CommandOutcome | None:
        return self.completed_outcomes.get(segment)

    def execute(self, segment: int, request: CommandRequest) -> CommandOutcome:
        self.requests.append((segment, request))
        return self.outcomes.pop(0)


@dataclass
class _Context:
    commands: _Commands
    runtime_values: dict[str, str] = field(default_factory=dict)
    root: Path = Path()
    workspace: Path | None = None
    task_id: str = "TASK-1"
    run_id: str | None = "01-task"
    operation_id: str = "op"
    attempt: int = 1
    extensions: Any = None


def _execute(planned: Commands, service: _Commands, **values: str):  # type: ignore[no-untyped-def]
    return CommandAction().execute(planned, _Context(service, values))  # type: ignore[arg-type]


def test_execute_joins_outputs_and_skips_completed_segments() -> None:
    planned = Commands(
        (
            CommandDefinition(argv=("first",)),
            CommandDefinition(argv=("second", "{{x}}")),
        )
    )
    service = _Commands(
        [CommandOutcome(True, stdout="two\n")],
        completed_outcomes={0: CommandOutcome(True, stdout="one")},
    )

    result = _execute(planned, service, x="value")

    assert result.ok
    assert result.output == "one\ntwo"
    assert service.requests == [(1, CommandRequest(("second", "value"), {}))]


@pytest.mark.parametrize(
    ("outcome", "error"),
    [
        (
            # The command is named and its output quoted, so the operator can
            # judge the failure without going to find the artifact.
            CommandOutcome(False, stderr=" boom \n", exit_code=2),
            "automatic handler failed (2) running: x\n\nboom",
        ),
        (
            # Diagnostics on stdout are what pytest, ruff and mypy produce.
            CommandOutcome(False, stdout="2 failed, 1 passed", exit_code=1),
            "automatic handler failed (1) running: x\n\n2 failed, 1 passed",
        ),
        (
            CommandOutcome(False, launch_error="not found"),
            "automatic handler could not launch command 1: not found",
        ),
    ],
)
def test_execute_reports_command_failures(outcome: CommandOutcome, error: str) -> None:
    result = _execute(Commands((CommandDefinition(argv=("x",)),)), _Commands([outcome]))

    assert not result.ok
    assert result.error == error


def test_execute_checks_the_assertion_against_all_output() -> None:
    planned = Commands(
        (CommandDefinition(argv=("x",)),),
        AssertionDefinition((AssertionCondition("equals", "expected"),)),
    )

    failed = _execute(planned, _Commands([CommandOutcome(True, stdout="actual")]))
    passed = _execute(planned, _Commands([CommandOutcome(True, stdout="expected\n")]))

    assert not failed.ok
    assert failed.error == (
        "automatic handler assertion failed: expected 'expected', got 'actual'"
    )
    assert failed.output == "actual"
    assert passed.ok


def test_execute_fails_before_launch_when_a_value_is_missing() -> None:
    service = _Commands([])

    result = _execute(Commands((CommandDefinition(argv=("{{x}}",)),)), service)

    assert not result.ok
    assert result.error == "automatic handler is missing variable(s): x"
    assert service.requests == []


# --- extension -------------------------------------------------------------------


@dataclass
class _Checker:
    outcome: object
    validated: list[Extension] = field(default_factory=list)

    def validate_identity(self, planned: Extension) -> None:
        self.validated.append(planned)

    def check(self, planned: Extension) -> object:
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


@dataclass
class _RecoveryContext:
    extensions: _Checker
    root: Path = Path()
    workspace: Path | None = None
    runtime_values: dict[str, str] = field(default_factory=dict)
    task_id: str = "TASK-1"
    run_id: str | None = "01-task"
    operation_id: str = "op"
    attempt: int = 1


_EXTENSION = Extension("ext/ww/git/handlers:commit")


def _check(outcome: object):  # type: ignore[no-untyped-def]
    checker = _Checker(outcome)
    result = actions.get("extension").check_recovery(
        _EXTENSION,
        _RecoveryContext(checker),  # type: ignore[arg-type]
    )
    return result, checker


def test_extension_checker_success_carries_the_extension_result() -> None:
    result, checker = _check(
        ExtensionCheckResult.succeeded(
            ExtensionResult(True, output="sha", values={"commit": "sha"})
        )
    )

    assert result.status == "succeeded"
    assert result.result is not None
    assert (result.result.output, dict(result.result.values)) == (
        "sha",
        {"commit": "sha"},
    )
    assert checker.validated == [_EXTENSION]


@pytest.mark.parametrize(
    ("outcome", "status", "error"),
    [
        (ExtensionCheckResult.not_succeeded(), "not_succeeded", ""),
        (
            ExtensionCheckResult.unknown("remote unreachable"),
            "unknown",
            "remote unreachable",
        ),
        ("not a result", "unknown", "extension checker returned an invalid result"),
        (RuntimeError("boom"), "unknown", "RuntimeError: boom"),
    ],
)
def test_extension_checker_outcomes(outcome: object, status: str, error: str) -> None:
    result, _ = _check(outcome)

    assert (result.status, result.error) == (status, error)


def test_extension_definition_and_instruction() -> None:
    action = actions.get("extension")

    assert action.parse({}, "ext/ww/git/handlers:commit", "", "p") == _EXTENSION
    assert action.templates(_EXTENSION) == ()
    with pytest.raises(ConfigurationError, match="invalid extension reference"):
        action.validate(Extension("not-a-reference"), "p")
    with pytest.raises(ConfigurationError, match="no extensions are loaded"):
        action.plan(_EXTENSION, _resolution())
    assert action.instruction(_EXTENSION, _context("Commit.")).text == "Commit."
    assert action.instruction(_EXTENSION, _context()).text == "step"
    assert action.traits(_EXTENSION).manual_attestation == {"values", "workspace"}


# --- extension input validation ---------------------------------------------------


@dataclass
class _Handlers:
    handler_: ExtensionHandler

    def handler(self, reference: str) -> ExtensionHandler:
        return self.handler_


@dataclass
class _InputContext:
    extensions: _Handlers


def _validate(validator: object, values: dict[str, str]) -> str | None:
    handler = ExtensionHandler(
        "publish",
        lambda context: ExtensionResult(True),
        provide=(ProvidedVariable("message"), ProvidedVariable("channel")),
        validate=validator,  # type: ignore[arg-type]
    )
    return actions.get("extension").validate_inputs(
        _EXTENSION,
        values,
        _InputContext(_Handlers(handler)),  # type: ignore[arg-type]
    )


def test_extension_without_a_validator_accepts_any_input() -> None:
    assert _validate(None, {"message": "a\nb"}) is None


def test_extension_validator_sees_only_its_own_declared_values() -> None:
    seen: list[dict[str, str]] = []

    def validator(values: object) -> None:
        seen.append(dict(values))  # type: ignore[call-overload]

    assert _validate(validator, {"message": "m", "other": "x"}) is None
    assert seen == [{"message": "m"}]


@pytest.mark.parametrize(
    ("validator", "error"),
    [
        (lambda values: "message must be one line", "message must be one line"),
        (lambda values: "  padded  ", "padded"),
        (lambda values: 42, "extension validator returned an invalid result"),
        (lambda values: "", "extension validator returned an invalid result"),
    ],
)
def test_extension_validator_verdicts(validator: object, error: str) -> None:
    assert _validate(validator, {"message": "a\nb"}) == error


def test_extension_validator_exceptions_are_refusals() -> None:
    def validator(values: object) -> None:
        raise RuntimeError("boom")

    assert _validate(validator, {"message": "m"}) == "RuntimeError: boom"
