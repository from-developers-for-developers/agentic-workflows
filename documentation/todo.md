# TODOs

Known open boundaries. None of them blocks the beta.

1. `WorkflowService` is large

`src/ww/service.py` is 3,874 lines; the `plan/` package is 2,610 lines, `config/`
3,237, and `instructions/` 3,298. Size alone is not a defect. The practical
issue is that init completion, assignment boundaries, and cross-task
publication interact inside the service without one obvious invariant
boundary, so a change to one of them has to be checked against the others by
reading.

Extract service components only around a cohesive responsibility with concrete
callers, and only when a change needs it; a cosmetic class split does not help.

2. Storage-adapter locking is easy to get wrong

The default `lock_task` and `lock_project_metadata` of a storage adapter are
no-ops, which is right for the process-local filesystem and memory adapters
but not for one shared between processes. Compare-and-swap on commit does not
replace the whole-operation lock: the coordinator fetches a fresh revision
when it commits. ww ships no shared adapter (see
[limitations.md](limitations.md#storage)), so this matters only to someone
writing one.

3. Task scans parse every task's state

`discover` parses every task's state to name the ones it cannot read. If a
repository gathers thousands of tasks, the fix is an index owned by the storage
adapter, written on every commit and rebuildable from the states.
