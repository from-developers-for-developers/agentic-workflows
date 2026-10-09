# SPDX-License-Identifier: GPL-3.0-or-later
"""Run recovery and external-storage-adapter compatibility release gates."""

from __future__ import annotations

import subprocess
import sys

GATE_TESTS = (
    # Every storage adapter must pass the same behavioral contract.
    "tests/unit/test_task_storage_adapters.py",
    # Unknown outcomes and partial command recovery must remain explicit.
    "tests/integration/test_plan_execution.py::test_interrupted_automatic_handler_requires_explicit_recovery",
    "tests/integration/test_plan_execution.py::test_cli_recovery_attests_one_handler_and_continues_with_the_next",
    "tests/integration/test_extension_execution.py::test_default_recovery_uses_extension_checker_tri_state",
    # A repeated completion request must not publish a second completion.
    "tests/integration/test_concurrency.py::test_parallel_completes_record_one_artifact_per_step",
    # Child creation must reconcile either side of an interrupted two-task update.
    "tests/integration/test_child_tasks.py::test_child_start_retries_after_parent_binding_was_persisted",
    "tests/integration/test_child_tasks.py::test_child_start_reconciles_published_child_after_parent_relink_failure",
    # A reset run gets new operation identities and never recovers an old effect.
    "tests/integration/test_plan_execution.py::test_reset_creates_a_new_operation_identity_for_the_same_run_name",
    "tests/integration/test_plan_execution.py::test_reset_does_not_recover_a_git_effect_from_the_previous_execution",
    # A finished child whose parent missed the news is reconciled from either side.
    "tests/integration/test_child_tasks.py::test_parent_status_repairs_a_missed_terminal_child_notification",
    "tests/integration/test_child_tasks.py::test_child_recover_repeats_a_missed_terminal_parent_notification",
    # Numeric task IDs keep counting, and status reads one aggregate revision.
    "tests/unit/test_task_ids.py::test_numeric_task_generation_continues_past_fifty",
    "tests/integration/test_workflow_service.py::test_status_uses_one_aggregate_revision",
    # A command that cannot launch is retryable; one that launched stays unknown.
    "tests/integration/test_plan_execution.py::test_process_creation_failure_is_a_retryable_cli_error",
    "tests/integration/test_plan_execution.py::test_error_after_process_creation_keeps_the_outcome_unknown",
    # Child-stage expansion must publish a complete, executable plan revision.
    "tests/integration/test_children_added_mid_run.py::test_the_added_child_runs_through_its_stages",
    "tests/integration/test_children_added_mid_run.py::test_a_snapshot_with_the_added_child_round_trips",
    # Extension state and lock cleanup retain their interleaving guarantees.
    "tests/unit/test_extensions.py::test_a_store_update_locks_the_complete_read_modify_write",
    "tests/unit/test_locking.py::test_lock_opens_sidecar_only_after_activity_gate",
    # Exercise discovery and execution from real installed distribution metadata.
    "tests/integration/test_packaged_extension.py",
)


def main() -> int:
    # Serially: under xdist a gate ID that no longer exists ends the run with
    # "no tests ran" instead of naming the missing test.
    return subprocess.call((sys.executable, "-m", "pytest", "-n", "0", *GATE_TESTS))


if __name__ == "__main__":
    raise SystemExit(main())
