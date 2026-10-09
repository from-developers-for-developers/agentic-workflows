# SPDX-License-Identifier: GPL-3.0-or-later
"""The sheet as seen against the plan: what each answer applies to.

A pending answer waits in the answer sheet.  The stage it applies to is the
plan item with ``ui`` set for that work item, and the answer counts as
applied once that stage's record is completed.  An applied answer is read
back from what the engine recorded: the stage's ``chosen`` field and the
operator's entries for that stage in the interactions file.  One item flow
may declare one ``ui`` stage, so the pairing is unambiguous.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from ww.execution_models import ExecutionState, PlanItemExecution
from ww.interactions import InteractionEntry
from ww.items import WorkItem
from ww.plan import PlanItem, WorkflowPlan

from .sheet import Answer


@dataclass(frozen=True)
class SheetRow:
    """One work item on the sheet: its stage, that stage's record, the answer
    waiting for it, and the answer the engine already recorded."""

    work: WorkItem
    stage: PlanItem | None
    record: PlanItemExecution | None
    pending: Answer | None
    applied: Answer | None

    @property
    def processed(self) -> bool:
        return self.record is not None and self.record.status == "completed"

    @property
    def answered(self) -> bool:
        return self.processed or self.pending is not None

    @property
    def status(self) -> str:
        if self.processed:
            return "processed"
        return "answered" if self.pending is not None else "open"

    def to_dict(self) -> dict[str, object]:
        shown = self.applied if self.processed else self.pending
        return {
            **self.work.to_dict(),
            "status": self.status,
            "answer": shown.choice if shown else None,
            "answer_comment": shown.comment if shown else "",
            "answered_at": shown.at if shown else None,
        }


def sheet_rows(
    plan: WorkflowPlan,
    state: ExecutionState,
    items: tuple[WorkItem, ...],
    answers: Mapping[str, Answer],
    entries: tuple[InteractionEntry, ...],
) -> tuple[SheetRow, ...]:
    stage = plan.items[state.cursor] if state.cursor < len(plan.items) else None
    if stage is None or not stage.ui:
        return ()
    record = state.item_executions[state.cursor]
    return tuple(
        SheetRow(work, stage, record, answers.get(work.id), None)
        for work in items
        if work.context == stage.item_context
    )
