# SPDX-License-Identifier: GPL-3.0-or-later
"""One ``interact --await``: apply what is pending, serve, wait, apply again.

The session drives the task only through the public service API an agent
uses.  Recording an answer on its stage is ``interact`` with the pick, the
comment, and the end; the built-in stage's item is marked with
``update_item``; the stage is finished with ``complete``; the next stage is
opened with ``next``.  Nothing here writes task state directly.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from ww.contracts import CallerRole
from ww.errors import StateError
from ww.execution_models import ExecutionState, PlanSnapshot
from ww.plan import PlanItem
from ww.service import WorkflowService, resolve_choice

from .server import (
    WaitOutcome,
    operator_page_port,
    serve_operator_page,
)
from .sheet import Answer, AnswerSheet
from .view import SheetRow, sheet_rows


@dataclass(frozen=True)
class OperatorPageResult:
    """How a wait ended and what was applied, for the agent to read."""

    outcome: WaitOutcome | None
    applied: tuple[str, ...]
    answered: int
    total: int
    paused: bool
    # Documents the answered step promises to update: ww cannot write them, so
    # recording the answers in the documents is the agent's to do.
    documents: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "outcome": self.outcome,
            "applied": list(self.applied),
            "answered": self.answered,
            "total": self.total,
            "paused": self.paused,
            "documents": list(self.documents),
        }

    def render(self) -> str:
        """A short Markdown block printed after the step's page."""
        ended = {
            "answered": "The operator answered every item.",
            "paused": "The operator said they are done for now.",
            "closed": "The operator closed the page.",
            "timed_out": "The wait passed with nothing new.",
            None: "Nothing was waited for.",
        }[self.outcome]
        applied = (
            "Recorded the answers of " + ", ".join(self.applied) + " as input. "
            "The agent must apply fields and explicit resolution/reporting "
            "transitions, then complete the step."
            if self.applied
            else "Nothing was applied."
        )
        progress = f"{self.answered} of {self.total} items are answered."
        if self.paused:
            advice = (
                "Stop here; do not wait again and do not delegate. When the "
                "operator returns, show the page with `instruction` and wait again."
            )
        elif self.outcome == "closed":
            advice = (
                "Do not open the page again on your own: ask the operator in the "
                "session whether to go on, and wait again only when they say so."
            )
        elif self.answered < self.total:
            advice = "Items remain; wait again."
        else:
            advice = "Every item is answered; go on with the page above."
        lines = ["## Operator page", "", f"{ended} {applied} {progress} {advice}"]
        if self.applied and self.documents:
            lines.extend(
                [
                    "",
                    "The step promises to update these documents, and "
                    "ww cannot write them: record the operator's answers for "
                    + ", ".join(self.applied)
                    + " in them now, before anything else.",
                    "",
                    *(f"- `{path}`" for path in self.documents),
                ]
            )
        return "\n".join([*lines, ""])


class _Session:
    def __init__(
        self, service: WorkflowService, task_id: str, caller_role: CallerRole | None
    ) -> None:
        self.service = service
        self.task_id = task_id
        self.caller_role = caller_role
        state, _snapshot = service.load(task_id)
        self.sheet = AnswerSheet(service.storage, task_id, state.created_at)
        # The documents the answered step promises to update, in order.
        self.documents: dict[str, None] = {}

    # -- reading ---------------------------------------------------------

    def load(self) -> tuple[ExecutionState, PlanSnapshot]:
        return self.service.load(self.task_id)

    def rows(
        self, state: ExecutionState, snapshot: PlanSnapshot
    ) -> tuple[SheetRow, ...]:
        return sheet_rows(
            snapshot.plan,
            state,
            self.service.tasks.read_items(self.task_id, state.run_id),
            self.sheet.read(self._sheet_scope(state, snapshot)),
            self.service.interactions.entries(self.task_id),
        )

    @staticmethod
    def _sheet_scope(state: ExecutionState, snapshot: PlanSnapshot) -> str:
        current = snapshot.plan.items[state.cursor]
        return f"{state.run_id}:{current.id}"

    def current_ui_stage(
        self, state: ExecutionState, snapshot: PlanSnapshot
    ) -> PlanItem:
        """The open ``ui`` stage, or why the page cannot be served."""
        plan = snapshot.plan
        if not state.active_item_id or state.cursor >= len(plan.items):
            raise StateError("no agent item is in progress; use next")
        item = plan.items[state.cursor]
        record = state.item_executions[state.cursor]
        if item.id != state.active_item_id or not item.ui:
            raise StateError(
                f"{item.name!r} is not answered on the operator page; the page "
                "serves items substeps declared with interactive: page"
            )
        if record.interaction_ended:
            raise StateError(
                f"the interaction of {item.name!r} has ended; complete the step"
            )
        return item

    def page_state(self) -> dict[str, object]:
        state, snapshot = self.load()
        plan = snapshot.plan
        current = plan.items[state.cursor] if state.cursor < len(plan.items) else None
        rows = self.rows(state, snapshot)
        stage = current if current and current.ui else None
        return {
            "task_id": self.task_id,
            "workflow": state.workflow,
            "run_id": state.run_id,
            "stage": stage.name if stage else None,
            "current_item_id": None,
            "context": current.item_context if current else None,
            "paused": state.operator_paused,
            "choices": [choice.to_dict() for choice in stage.choices] if stage else [],
            "items": [row.to_dict() for row in rows],
            "answered": sum(1 for row in rows if row.answered),
            "total": len(rows),
        }

    # -- the operator's actions -------------------------------------------

    def act(self, payload: dict[str, object]) -> WaitOutcome | None:
        action = payload.get("action")
        comment = payload.get("comment")
        choice = payload.get("choice")
        item_id = payload.get("item_id")
        if not all(
            value is None or isinstance(value, str)
            for value in (comment, choice, item_id)
        ):
            raise StateError("the answer's item, choice, and comment must be strings")
        match action:
            case "answer":
                if not item_id:
                    raise StateError("an answer names its item")
                complete = self.answer(
                    str(item_id),
                    str(choice).strip() if choice else None,
                    str(comment).strip() if comment else "",
                )
                return "answered" if complete else None
            case "pause":
                # Recorded by the session once the answers given before it
                # are applied, so applying does not lift the pause.
                return "paused"
            case _:
                raise StateError(f"unknown operator action {action!r}")

    def answer(self, item_id: str, choice: str | None, comment: str) -> bool:
        """Put one answer on the sheet; true when every item has one."""
        with self.service.tasks.lock_task(self.task_id):
            state, snapshot = self.load()
            rows = self.rows(state, snapshot)
            row = next((row for row in rows if row.work.id == item_id), None)
            if row is None:
                raise StateError(f"item {item_id!r} was not found")
            if row.stage is None:
                raise StateError(f"item {item_id!r} has no stage answered on the page")
            if row.processed:
                raise StateError(
                    f"the answer of {item_id!r} was already applied; it cannot change"
                )
            if row.stage.choices:
                if choice is None:
                    raise StateError("pick one of the choices")
                choice = resolve_choice(row.stage, choice)
            elif not comment:
                raise StateError("write a comment")
            else:
                choice = None
            self.sheet.record(
                self._sheet_scope(state, snapshot),
                item_id,
                Answer(choice, comment, _now()),
            )
            complete = all(
                row.processed or row.pending or row.work.id == item_id for row in rows
            )
            returned = state.operator_paused
        if returned:
            # Answering is the operator coming back; recorded like any other
            # word of theirs, outside the lock since interact takes it.
            self.service.interact(
                self.task_id,
                operator=f"Answered {item_id} on the operator page.",
                caller_role=self.caller_role,
            )
        return complete

    # -- applying ----------------------------------------------------------

    def apply_pending(self) -> tuple[str, ...]:
        """Record the completed answer sheet as input; the agent owns outcomes."""
        state, snapshot = self.load()
        if state.cursor >= len(snapshot.plan.items):
            return ()
        current = snapshot.plan.items[state.cursor]
        record = state.item_executions[state.cursor]
        if not current.ui or record.interaction_ended:
            return ()
        rows = self.rows(state, snapshot)
        if not rows or not all(row.pending is not None for row in rows):
            return ()
        page = self.service.instruction(self.task_id, caller_role=self.caller_role)
        for document in page.documents:
            self.documents[document.path] = None
        text = "Operator answers (evidence only):\n" + "\n".join(
            f"{row.work.id}: {_outcome_text(row.pending)}"
            for row in rows
            if row.pending is not None
        )
        self.service.interact(
            self.task_id, operator=text, end=True, caller_role=self.caller_role
        )
        return tuple(row.work.id for row in rows)

    def record_pause(self) -> None:
        """The operator said they are done for now, on the stage now open."""
        state, snapshot = self.load()
        plan = snapshot.plan
        if (
            state.cursor < len(plan.items)
            and state.active_item_id
            and (plan.items[state.cursor].interactive)
        ):
            self.service.interact(
                self.task_id, pause=True, caller_role=self.caller_role
            )

    def result(
        self, outcome: WaitOutcome | None, applied: tuple[str, ...]
    ) -> OperatorPageResult:
        state, snapshot = self.load()
        rows = self.rows(state, snapshot)
        return OperatorPageResult(
            outcome,
            applied,
            sum(1 for row in rows if row.answered),
            len(rows),
            state.operator_paused,
            tuple(self.documents),
        )


def run_operator_page(
    service: WorkflowService,
    task_id: str,
    *,
    timeout: float,
    open_browser: Callable[[str], object] | None,
    caller_role: CallerRole | None = None,
) -> OperatorPageResult:
    """Apply leftover answers, serve the page until the wait ends, apply again."""
    session = _Session(service, task_id, caller_role)
    applied = session.apply_pending()
    state, snapshot = session.load()
    try:
        session.current_ui_stage(state, snapshot)
    except StateError:
        if applied:
            # The answers ended the step's interaction; nothing to wait for.
            return session.result(None, applied)
        raise
    outcome = serve_operator_page(
        port=operator_page_port(task_id),
        state=session.page_state,
        act=session.act,
        timeout=timeout,
        open_browser=open_browser,
    )
    applied += session.apply_pending()
    if outcome == "paused":
        session.record_pause()
    return session.result(outcome, applied)


def _outcome_text(answer: Answer) -> str:
    """The operator's answer as recorded evidence for one item."""
    if answer.choice and answer.comment:
        return f"{answer.choice}: {answer.comment}"
    return answer.choice or answer.comment


def _now() -> str:
    stamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    return stamp.replace("+00:00", "Z")
