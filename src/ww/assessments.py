# SPDX-License-Identifier: GPL-3.0-or-later
"""The outcomes of an ``assess`` step, and when one is waiting to be chosen.

An assessment is completed like any agent step; its answer is then given to
``next --outcome``. The transition that applies the answer and the pages that
ask for it both read the choice from here, so they cannot disagree about which
answers exist or what each one does.
"""

from __future__ import annotations

from dataclasses import dataclass

from ww.errors import StateError
from ww.execution_models import ExecutionState
from ww.plan import PlanItem, WorkflowPlan

# The answers every assessment with declared outcomes accepts. One it does not
# declare runs nothing and continues after the assessment, so a gate declares
# only the outcome that has work. The compact form keeps its own rule:
# positive continues, negative ends the workflow.
STANDARD_OUTCOMES = ("positive", "negative", "mixed")


@dataclass(frozen=True)
class AssessmentOutcome:
    """One answer: the step it runs first, or that it ends the workflow."""

    label: str
    stops: bool = False
    first_step: str | None = None
    # A standard answer the assessment does not declare: it skips every
    # outcome's work and continues after the assessment.
    declared: bool = True


@dataclass(frozen=True)
class PendingAssessment:
    """A completed assessment whose outcome ``next`` must be given."""

    index: int
    question: str
    outcomes: tuple[AssessmentOutcome, ...]
    # Declared outcomes, as opposed to the compact form.
    declared: bool

    @property
    def labels(self) -> tuple[str, ...]:
        return tuple(outcome.label for outcome in self.outcomes)

    def outcome(self, label: str) -> AssessmentOutcome | None:
        return next((item for item in self.outcomes if item.label == label), None)


def assessment_outcomes(
    plan: WorkflowPlan, index: int
) -> tuple[AssessmentOutcome, ...]:
    """What each answer to the assessment at ``index`` does."""
    item = plan.items[index]
    if not item.assessment_outcomes:
        following = plan.items[index + 1].name if index + 1 < len(plan.items) else None
        return (
            AssessmentOutcome("positive", first_step=following),
            AssessmentOutcome("negative", stops=True),
        )
    region = outcome_region(plan, index)
    after = max(region, default=index) + 1
    declared = tuple(
        AssessmentOutcome(
            label,
            stops=label in item.assessment_stops,
            first_step=next(
                (
                    plan.items[position].name
                    for position in region
                    if plan.items[position].assessment_outcome == label
                ),
                None,
            ),
        )
        for label in item.assessment_outcomes
    )
    implied = tuple(
        AssessmentOutcome(
            label,
            first_step=plan.items[after].name if after < len(plan.items) else None,
            declared=False,
        )
        for label in STANDARD_OUTCOMES
        if label not in item.assessment_outcomes
    )
    return (*declared, *implied)


def outcome_region(plan: WorkflowPlan, index: int) -> tuple[int, ...]:
    """The plan positions of every declared outcome's work, in order."""
    step = plan.items[index].step
    return tuple(
        position
        for position, candidate in enumerate(plan.items)
        if _assessment_parent(candidate) == step
    )


def pending_assessment(
    state: ExecutionState, plan: WorkflowPlan
) -> PendingAssessment | None:
    """The assessment just completed whose outcome is still to be chosen.

    Its outcome items, or for the compact form the step after it, are still
    pending at the cursor; once an outcome is chosen the cursor moves into it
    and nothing is pending here any more.
    """
    if not 0 < state.cursor < len(plan.items):
        return None
    if state.item_executions[state.cursor].status != "pending":
        return None
    item = plan.items[state.cursor]
    parent = _assessment_parent(item)
    if parent is not None:
        group = [
            index
            for index, candidate in enumerate(plan.items)
            if _assessment_parent(candidate) == parent
        ]
        if state.cursor != min(group):
            return None
        index = next(
            (
                index
                for index, candidate in enumerate(plan.items)
                if candidate.step == parent
                and candidate.assessment_question is not None
            ),
            None,
        )
        if index is None:
            raise StateError(f"assessment outcome has no parent assessment: {parent}")
        return _pending(plan, index, declared=True)
    previous = plan.items[state.cursor - 1]
    if previous.assessment_question is not None and not previous.assessment_outcomes:
        return _pending(plan, state.cursor - 1, declared=False)
    return None


def _pending(plan: WorkflowPlan, index: int, *, declared: bool) -> PendingAssessment:
    question = plan.items[index].assessment_question
    assert question is not None  # pragma: no cover - callers check it
    return PendingAssessment(
        index, question, assessment_outcomes(plan, index), declared
    )


def _assessment_parent(item: PlanItem) -> str | None:
    """Resolve template paths retained by previously materialized snapshots.

    Keep the persisted plan unchanged so its digest and completed work remain
    valid. Concrete paths bind each outcome to its own item or child.
    """
    parent = item.assessment_parent
    if parent is None:
        return None
    parts = parent.split("/")
    step_parts = item.step.split("/")
    for index, part in enumerate(parts):
        if part in {"{item}", "{child}"} and index < len(step_parts):
            parts[index] = step_parts[index]
    return "/".join(parts)
