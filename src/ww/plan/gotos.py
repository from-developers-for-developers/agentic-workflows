# SPDX-License-Identifier: GPL-3.0-or-later
"""The gotos a workflow allows, checked against its compiled plan.

A goto sends the flow from one step to another, back to a step already run
or ahead past the steps between. Assessment outcomes and verification
failure branches declare every permitted edge. Targets name full step paths
and are validated once the compiled plan identifies all existing paths.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

from ww.errors import ConfigurationError
from ww.operations import ChildWorkflowRun
from ww.workflow_config import StepDefinition, WorkflowDefinition

from .models import PlanItem, PlannedGoto

INIT_STEP = "init"
# The value a goto's ``detail`` may read, and only it: the asking step's path.
GOTO_FROM = "{{ww.goto.from}}"
DETAIL_REFERENCE = re.compile(r"\{\{.*?\}\}")
# The phases that belong to a step: its own item and its hooks. A workflow's
# start and completion hooks carry the first or last step's path, but are not
# that step's.
STEP_PHASES = frozenset({"before_start", "step", "before_complete", "after_complete"})


def in_step(item: PlanItem, path: str) -> bool:
    """Whether ``item`` is ``path``'s own work: the step, its hooks, its substeps."""
    return item.phase in STEP_PHASES and (
        item.step == path or item.step.startswith(f"{path}/")
    )


def step_span(items: tuple[PlanItem, ...], path: str) -> tuple[int, int] | None:
    """The first and last plan positions of ``path``'s work, if it has any."""
    found = [index for index, item in enumerate(items) if in_step(item, path)]
    return (found[0], found[-1]) if found else None


def planned_gotos(
    workflow: WorkflowDefinition, items: tuple[PlanItem, ...]
) -> tuple[PlannedGoto, ...]:
    """Every goto of ``workflow``, each checked against the plan's ``items``."""
    gotos = [
        *(
            PlannedGoto(path, outcome.goto.target, outcome.name, outcome.goto.detail)
            for path, step in _paths(workflow.steps, None)
            for outcome in step.assessment_outcomes
            if outcome.goto is not None
        ),
        *(
            PlannedGoto(
                path,
                step.verification.goto.target,
                "__verification_failure",
                step.verification.goto.detail,
            )
            for path, step in _paths(workflow.steps, None)
            if step.verification is not None and step.verification.goto is not None
        ),
    ]
    for goto in gotos:
        _check(workflow.name, goto, items)
    return tuple(gotos)


def _paths(
    steps: tuple[StepDefinition, ...], parent: str | None
) -> Iterator[tuple[str, StepDefinition]]:
    """Each step with its plan path, as the compiler names it."""
    for step in steps:
        path = f"{parent}/{step.name}" if parent else step.name
        yield path, step
        yield from _paths(step.child_steps, path)
        yield from _paths(step.assessment_outcomes, path)
        if step.items is not None:
            yield from _paths(step.items.steps, path)
        if step.children is not None:
            yield from _paths(step.children.steps, f"{path}/{{child}}")


def _check(workflow: str, goto: PlannedGoto, items: tuple[PlanItem, ...]) -> None:
    label = (
        f"{workflow}/{goto.source}.outcomes.{goto.outcome}.goto"
        if goto.outcome is not None
        else f"{workflow}/{goto.source}.goto"
    )
    source = next(
        (
            item
            for item in items
            if item.step == goto.source
            and item.phase == "step"
            and item.owner == "agent"
            and item.verifies is None
            and not item.summary
        ),
        None,
    )
    if source is None:
        raise ConfigurationError(
            f"{label}: {goto.source!r} is not an agent step of the workflow; "
            "name a step by its full path, such as develop/fix"
        )
    target = step_span(items, goto.target)
    if target is None:
        raise ConfigurationError(
            f"{label}: {goto.target!r} is not a step of the workflow; name a "
            "step by its full path, such as develop/fix"
        )
    for path in (goto.source, goto.target):
        if path == INIT_STEP:
            raise ConfigurationError(f"{label}: init takes no goto")
        if any(
            in_step(item, path) and (item.child_stage or item.child_template)
            for item in items
        ):
            raise ConfigurationError(
                f"{label}: {path!r} is inside children, which takes no goto"
            )
    if goto.detail is not None:
        unknown = sorted(
            {
                reference.replace(" ", "")
                for reference in DETAIL_REFERENCE.findall(goto.detail)
            }
            - {GOTO_FROM}
        )
        if unknown:
            raise ConfigurationError(
                f"{label}: the detail reads {unknown[0]}; a goto's detail reads "
                f"only {GOTO_FROM}; the source summary is always passed"
            )
    if goto.target == goto.source or goto.target.startswith(f"{goto.source}/"):
        raise ConfigurationError(
            f"{label}: a step cannot go to itself or to a step inside it"
        )
    if _items_body(items, goto.source) != _items_body(items, goto.target):
        raise ConfigurationError(
            f"{label}: {goto.source!r} and {goto.target!r} are not in the same "
            "items body; a goto cannot cross into or out of one"
        )
    source_span = step_span(items, goto.source)
    assert source_span is not None  # its step item was found above
    if target[0] <= source_span[0] and any(
        item.child_operation is not None or isinstance(item.operation, ChildWorkflowRun)
        for item in items[target[0] : source_span[1] + 1]
    ):
        raise ConfigurationError(
            f"{label}: the steps from {goto.target!r} to {goto.source!r} hold "
            "children, which a goto back cannot run again"
        )


def _items_body(items: tuple[PlanItem, ...], path: str) -> str | None:
    """The items step whose body holds ``path``, if one does."""
    bodies = {item.item_context for item in items if item.item_context}
    inside = [body for body in bodies if path.startswith(f"{body}/")]
    return max(inside, key=len, default=None)
