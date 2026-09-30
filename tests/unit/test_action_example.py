# SPDX-License-Identifier: GPL-3.0-or-later
"""Copyable instruction action with a private payload and no built-in parent."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.action_contract_helpers import assert_action_contract
from tests.workflow_helpers import start_after_init
from ww.actions import (
    Action,
    InstructionContent,
    InstructionContext,
    ResolutionContext,
    actions,
)
from ww.discovery import AvailableActions
from ww.errors import ConfigurationError
from ww.interpolation import interpolate
from ww.service import WorkflowService
from ww.storage import Storage


@dataclass(frozen=True)
class Checklist:
    checks: tuple[str, ...]


class ChecklistAction(Action[Checklist, Checklist]):
    identifier = "example_checklist"
    planned_type = Checklist

    def parse(
        self, source: dict[str, object], name: str, description: str, path: str
    ) -> Checklist:
        try:
            return self.decode(source)
        except ValueError as error:
            raise ConfigurationError(f"{path}: {error}") from error

    def validate(self, definition: Checklist, path: str) -> None:
        if not definition.checks or any(
            not check.strip() for check in definition.checks
        ):
            raise ConfigurationError(f"{path}: checklist requires non-empty checks")

    def templates(self, definition: Checklist) -> tuple[str, ...]:
        return definition.checks

    def plan(self, definition: Checklist, context: ResolutionContext) -> Checklist:
        return Checklist(
            tuple(context.interpolate(check) for check in definition.checks)
        )

    def instruction(
        self, planned: Checklist, context: InstructionContext
    ) -> InstructionContent:
        lines = tuple(
            f"- [ ] {interpolate(check, context.task_values)}"
            for check in planned.checks
        )
        return InstructionContent("\n".join(lines), lines)

    def encode(self, planned: Checklist) -> dict[str, object]:
        return {"checks": list(planned.checks)}

    def decode(self, data: dict[str, object]) -> Checklist:
        checks = data.get("checks")
        if (
            set(data) != {"checks"}
            or not isinstance(checks, list)
            or not checks
            or any(not isinstance(check, str) or not check.strip() for check in checks)
        ):
            raise ValueError("checks must be a non-empty list of non-empty strings")
        return Checklist(tuple(checks))


def test_checklist_contract() -> None:
    planned = assert_action_contract(
        ChecklistAction(),
        {"checks": ["Inspect {{ww.task.id}}", "Run tests"]},
        ResolutionContext(
            agent="codex",
            available=AvailableActions(frozenset(), frozenset()),
            extensions=None,
            builtins={"ww.task.id": "EXAMPLE"},
            allowed_variables=frozenset({"ww.task.id"}),
        ),
        InstructionContext(description="", name="review", task_values={}),
    )
    assert planned == Checklist(("Inspect EXAMPLE", "Run tests"))


@pytest.mark.parametrize("checks", [[], [""], [3], "a check"])
def test_checklist_rejects_invalid_fields(checks: object) -> None:
    with pytest.raises(ConfigurationError, match="checks must"):
        ChecklistAction().parse({"checks": checks}, "review", "", "action")


def test_checklist_registration_works_through_saved_workflow(tmp_path: Path) -> None:
    actions.register(ChecklistAction())
    try:
        (tmp_path / "ww-agentic-workflows.yaml").write_text(
            """workflows:
  - name: task
    steps:
      - name: review
        action:
          type: example_checklist
          checks: ["Inspect {{ww.task.id}}", "Run tests"]
""",
            encoding="utf-8",
        )
        service = WorkflowService(Storage(tmp_path))
        start_after_init(service, "task", "EXAMPLE", agent="codex")
        instruction = service.next("EXAMPLE")
        assert instruction.action_text == "- [ ] Inspect EXAMPLE\n- [ ] Run tests"
        # A new service reads and decodes the saved payload through registration.
        resumed = WorkflowService(Storage(tmp_path))
        assert resumed.instruction("EXAMPLE").action_text == instruction.action_text
        resumed.complete(
            "EXAMPLE",
            artifact="Both checks passed.",
            summary_for_next="Done.",
        )
        resumed.next("EXAMPLE")
        result = resumed.complete(
            "EXAMPLE",
            (("summary", "Checklist completed."),),
            summary_for_next="Done.",
        )
        assert result.status == "completed"
    finally:
        actions.unregister("example_checklist")
