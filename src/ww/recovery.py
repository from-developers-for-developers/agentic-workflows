# SPDX-License-Identifier: GPL-3.0-or-later
"""Recovery of automatic items whose external outcome is unknown.

An ``in_progress`` record for an automatic item is written immediately before
the external command or extension runs.  Finding one during a later command
means the process died mid-operation.  Nothing here replays such an operation
implicitly: ``retry`` is an explicit decision, ``mark_succeeded`` records an
operator's attestation, and a recovery checker may settle the outcome when the
action provides one.
"""

from __future__ import annotations

from dataclasses import dataclass

from ww.action_execution import (
    STATE_OUTPUT_PREVIEW_LIMIT,
    ActionExecutor,
    bounded,
    extension_output_error,
)
from ww.actions import (
    Action,
    ActionResult,
    ActionTraits,
    AutomaticAction,
    PlannedAction,
    actions,
)
from ww.completion_inputs import validate_values
from ww.control import replays_harmlessly
from ww.errors import StateError
from ww.execution_models import ExecutionState, PlanItemExecution, PlanSnapshot
from ww.instructions import Instruction
from ww.plan import PlanItem
from ww.run_coordination import RunLifecycle
from ww.storage_adapters import CommandOutputAddress, TaskStorageAdapter
from ww.task_ids import validate_task_id
from ww.transitions import (
    Clock,
    attest_cli_command,
    attest_extension_item,
    record_recovery_uncertainty,
    resume_interrupted_item,
    retry_failed_item,
)
from ww.workspace import relative_workspace


@dataclass(frozen=True)
class _Interrupted:
    """The interrupted automatic item a recovery command addresses."""

    state: ExecutionState
    snapshot: PlanSnapshot
    item: PlanItem
    record: PlanItemExecution
    implementation: Action[object, object]
    planned: object
    traits: ActionTraits


@dataclass
class _Decision:
    """Operator flags, refined by a recovery checker before they are applied."""

    retry: bool
    mark_succeeded: bool
    output: str | None
    working_directory: str | None
    values: dict[str, str]
    checker_result: ActionResult | None = None

    @property
    def inspecting(self) -> bool:
        return not self.retry and not self.mark_succeeded


class RecoveryCoordinator:
    def __init__(
        self,
        tasks: TaskStorageAdapter,
        executor: ActionExecutor,
        lifecycle: RunLifecycle,
        now: Clock,
    ) -> None:
        self.tasks = tasks
        self.executor = executor
        self.lifecycle = lifecycle
        self.now = now

    def recover(
        self,
        task_id: str,
        *,
        retry: bool = False,
        mark_succeeded: bool = False,
        output: str | None = None,
        working_directory: str | None = None,
        variables: tuple[tuple[str, str], ...] = (),
    ) -> Instruction:
        """Inspect or explicitly resolve an interrupted automatic item.

        Without flags this only reports the recovery boundary.  ``retry``
        knowingly replays the unfinished operation; ``mark_succeeded`` records
        that an operator verified its external effect and advances the plan
        without running it again.
        """
        if retry and mark_succeeded:
            raise StateError("choose only one of --retry or --mark-succeeded")
        if (
            output is not None or working_directory is not None or variables
        ) and not mark_succeeded:
            raise StateError(
                "--output, --working-directory and --variable require --mark-succeeded"
            )
        validate_task_id(task_id)
        with self.tasks.lock_task(task_id):
            state, snapshot = self.lifecycle.load(task_id)
            state = self._interrupt_stale_operation(state, snapshot)
            if state.status == "failed" and retry:
                # A failed handler has a known outcome and uses the normal
                # retry transition rather than interrupted-operation recovery.
                return self.lifecycle.resume(
                    retry_failed_item(state, snapshot.plan, self.now), snapshot
                )
            if state.status != "interrupted":
                if not retry and not mark_succeeded:
                    # A concurrent invocation may have advanced the operation
                    # while this command waited for the lock.
                    return self.lifecycle.render(state, snapshot)
                raise StateError("task has no interrupted automatic handler")
            target = self._interrupted_target(state, snapshot)
            decision = _Decision(
                retry,
                mark_succeeded,
                output,
                working_directory,
                validate_values(variables),
            )
            self._check_attestation_support(target, decision)
            if decision.inspecting:
                replayed = self.replay_if_idempotent(state, snapshot)
                if replayed is not None:
                    return self.lifecycle.resume(replayed, snapshot)
                if self._command_boundaries_known(target):
                    # Every durable command boundary is known: resume pending
                    # segments without asking the operator to attest anything.
                    return self.lifecycle.resume(
                        resume_interrupted_item(
                            state,
                            snapshot.plan,
                            target.item,
                            now=self.now,
                        ),
                        snapshot,
                    )
                if isinstance(target.implementation, AutomaticAction):
                    settled = self._consult_checker(target, decision)
                    if settled is not None:
                        return settled
            if decision.inspecting:
                # No general-purpose attestation applies; the unknown outcome
                # stays visible until the operator makes an explicit choice.
                return self.lifecycle.render(state, snapshot)
            if decision.mark_succeeded:
                state = self._attest(target, decision)
            else:
                state = resume_interrupted_item(
                    state,
                    snapshot.plan,
                    target.item,
                    now=self.now,
                )
            return self.lifecycle.resume(state, snapshot)

    def replay_if_idempotent(
        self, state: ExecutionState, snapshot: PlanSnapshot
    ) -> ExecutionState | None:
        """Return an interrupted item to pending when its replay is declared harmless.

        ``idempotent: true`` on a command handler is the author's statement
        that running the sequence again cannot do damage, so the unknown
        outcome needs no operator: the interrupted and unrun segments run
        again under the same operation identity.  Anything else returns
        ``None`` and stays at the recovery boundary.
        """
        if state.status != "interrupted" or state.cursor >= len(snapshot.plan.items):
            return None
        item = snapshot.plan.items[state.cursor]
        record = state.item_executions[state.cursor]
        if record.status != "interrupted" or not replays_harmlessly(item):
            return None
        return resume_interrupted_item(
            state,
            snapshot.plan,
            item,
            now=self.now,
        )

    def _interrupt_stale_operation(
        self, state: ExecutionState, snapshot: PlanSnapshot
    ) -> ExecutionState:
        """Mark an automatic item still recorded as running by a dead process."""
        if (
            state.status == "interrupted"
            or not state.active_item_id
            or state.cursor >= len(snapshot.plan.items)
        ):
            return state
        item = snapshot.plan.items[state.cursor]
        if (
            item.owner == "ww"
            and item.execution == "automatic"
            and state.item_executions[state.cursor].status == "in_progress"
        ):
            return self.lifecycle.mark_interrupted(state, snapshot)
        return state

    @staticmethod
    def _interrupted_target(
        state: ExecutionState, snapshot: PlanSnapshot
    ) -> _Interrupted:
        if state.cursor >= len(snapshot.plan.items):
            raise StateError("interrupted task has no current item to recover")
        item = snapshot.plan.items[state.cursor]
        record = state.item_executions[state.cursor]
        if item.id != state.active_item_id or record.status != "interrupted":
            raise StateError("interrupted task has an inconsistent active item")
        if not isinstance(item.operation, PlannedAction):
            raise StateError(
                "interrupted core operation cannot be recovered as an action"
            )
        implementation = actions.get(item.kind)
        planned = item.payload_as(implementation.planned_type)
        return _Interrupted(
            state,
            snapshot,
            item,
            record,
            implementation,
            planned,
            implementation.traits(planned),
        )

    @staticmethod
    def _check_attestation_support(target: _Interrupted, decision: _Decision) -> None:
        attestable = target.traits.manual_attestation
        if decision.working_directory is not None and "workspace" not in attestable:
            raise StateError(
                "--working-directory is not supported for this action's manual "
                "recovery attestation"
            )
        if decision.values and "values" not in attestable:
            raise StateError(
                "--variable is not supported for this action's manual recovery "
                "attestation"
            )
        if (
            decision.mark_succeeded
            and target.traits.attests_output
            and decision.output is None
        ):
            raise StateError(
                "--output is required to attest a CLI command with an assertion"
            )

    @staticmethod
    def _command_boundaries_known(target: _Interrupted) -> bool:
        return (
            target.traits.command_segments is not None
            and bool(target.record.commands)
            and not any(
                command.status == "interrupted" for command in target.record.commands
            )
        )

    def _consult_checker(
        self, target: _Interrupted, decision: _Decision
    ) -> Instruction | None:
        """Let the action's recovery checker settle or refine the decision.

        Returns an instruction when the checker fully resolved the item (a
        segment was attested, or the outcome stays uncertain); otherwise it
        updates ``decision`` and returns ``None`` so the caller applies it.
        """
        checked = self.executor.check_recovery(
            target.state, target.snapshot.plan, target.item
        )
        if checked is None:
            return None
        if checked.status == "succeeded" and checked.result is not None:
            decision.checker_result = checked.result
            if checked.scope == "action":
                decision.mark_succeeded = True
                if (
                    decision.working_directory is None
                    and checked.result.working_directory is not None
                ):
                    decision.working_directory = str(checked.result.working_directory)
                return None
            if checked.segment is None:
                raise StateError("checker attested a command segment without naming it")
            state = self._attest_command_segment(
                target, checked.segment, checked.result.output
            )
            return self.lifecycle.resume(state, target.snapshot)
        if checked.status == "not_succeeded":
            decision.retry = True
            return None
        error = checked.error or (
            "extension checker could not establish the operation outcome"
        )
        state = record_recovery_uncertainty(target.state, error, self.now)
        self.lifecycle.commit(state, target.snapshot)
        return self.lifecycle.render(state, target.snapshot)

    def _attest(self, target: _Interrupted, decision: _Decision) -> ExecutionState:
        """Apply an operator's or checker's success attestation."""
        result = decision.checker_result
        if target.traits.command_segments is not None and result is None:
            # An operator attests one command boundary; only output applies.
            if decision.values or decision.working_directory is not None:
                raise StateError(
                    "a command-segment attestation accepts output only; "
                    "values and working directory require an action-level "
                    "manual attestation"
                )
            interrupted_index = next(
                (
                    index
                    for index, command in enumerate(target.record.commands)
                    if command.status == "interrupted"
                ),
                None,
            )
            if interrupted_index is None:
                raise StateError("interrupted CLI item has no command segment")
            return self._attest_command_segment(
                target, interrupted_index, decision.output or ""
            )
        # A checker that declared action scope attests the complete action, so
        # its output, values, and directory apply through the action-result path.
        result_values = dict(result.values) if result is not None else decision.values
        result_error = extension_output_error(result_values, target.item.outputs)
        if result_error is not None:
            raise StateError("cannot mark action succeeded: " + result_error)
        return attest_extension_item(
            target.state,
            target.snapshot.plan,
            result_values,
            (
                decision.output
                if decision.output is not None
                else target.record.result
                or (result.output if result else "")
                or "marked succeeded after interruption"
            ),
            (
                relative_workspace(self.executor.root, decision.working_directory)
                if decision.working_directory is not None
                else None
            ),
            self.now,
        )

    def _attest_command_segment(
        self, target: _Interrupted, segment: int, output: str
    ) -> ExecutionState:
        """Persist a segment attestation, then let normal command resumption run."""
        record = target.record
        if not 0 <= segment < len(record.commands):
            raise StateError("checker identified an invalid command segment")
        command = record.commands[segment]
        state = target.state
        stdout_ref = (
            self.tasks.write_command_output(
                CommandOutputAddress(
                    state.task_id,
                    state.run_id or state.workflow,
                    target.item.id,
                    command.operation_id
                    or f"{record.operation_id}:command:{segment + 1}",
                    max(command.attempts, 1),
                    segment + 1,
                    "stdout",
                ),
                output,
            )
            if output
            else None
        )
        return attest_cli_command(
            state,
            target.snapshot.plan,
            target.item,
            segment,
            bounded(output, STATE_OUTPUT_PREVIEW_LIMIT),
            stdout_ref,
            self.now,
        )
