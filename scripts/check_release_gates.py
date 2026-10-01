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
    # Follow-up regressions cover reset identities, terminal child recovery,
    # numeric allocation, consistent reads, and process-creation failures.
    "tests/integration/test_review_followup.py",
    # Dynamic expansion must publish a complete executable plan revision.
    "tests/integration/test_items.py::test_item_materialization_is_a_complete_executable_plan_revision",
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
