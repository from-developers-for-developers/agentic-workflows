# Python and architecture rules for agents

Purpose: keep ww understandable, resumable, extensible, and conventional Python. Apply these rules to the requested change. They are engineering guidance, not authorization for a repository-wide rewrite or removal of supported behavior. Explicit user instructions and the repository's workflow instructions remain authoritative.

## 1. Understand the contract before editing

- Trace the relevant input, domain model, execution path, persistence, and tests. Read the nearest documentation, but verify claims against code.
- State the observable behavior being changed and the invariants that must remain true. Distinguish a defect, a compatibility requirement, and a preference.
- Preserve unrelated work. Do not bundle speculative features or broad cleanup into a focused fix.

## 2. Keep parse → plan → execute separate

- Frontends interpret their source notation and produce normalized definitions. Shared semantic validation applies to every frontend.
- Compilation resolves actions, hook order, ownership, and data dependencies into an explicit plan.
- Execution advances the saved plan. It must not reinterpret YAML, rediscover hook order, or silently recompile an active run.
- Dynamic materialization must produce a consistent, versioned plan/state revision. Keep the original template and its execution interpretation distinguishable.
- Rendering presents a view; it must not select work, advance state, or create execution rules.

## 3. Choose the smallest useful abstraction

- Start with clear functions and ordinary data structures. Use a class for cohesive state/behavior, a dataclass for a record, and a module for related operations.
- Add an interface at a real boundary: persistence, external execution, or another current integration requirement. A stated extension requirement can justify a seam before a second implementation exists.
- Before adding a layer, name the concrete caller, variation, or invariant it isolates. Avoid pass-through managers, registries, factories, and deep inheritance without that purpose.
- Share code when it implements the same rule. Similar-looking code with different semantics need not share an abstraction.
- Do not impose arbitrary file-size limits or require a design pattern everywhere. Split by responsibility and reasons to change.

## 4. Make domain rules explicit

- Use specific types for identifiers, status sets, and action payloads where they prevent mistakes. Prefer `Literal`, enums, dataclasses, and small unions over loosely related flags.
- Do not allow action kind, owner, execution mode, and payload to contradict one another. Derive redundant fields or validate their relationship centrally.
- Give large record constructors keyword arguments. Use `dataclasses.replace` for same-type updates; avoid copying `__dict__` as a construction or schema strategy.
- Keep `Any` near genuinely untyped input. Narrow and validate it before it enters execution logic. Type public interfaces fully.
- Frozen records may contain mutable values. Choose and document ownership; copy or freeze nested data when callers must not mutate shared configuration.

## 5. Make configuration truthful

- Every accepted option must have a documented effect or an explicit deprecation/guidance status. Do not silently discard behavior-looking inputs.
- Reject unsupported and malformed authored input with useful field paths. Normalize shorthand once at the frontend boundary.
- Do not invent syntax when ordinary YAML fields, strings, and lists express the same thing clearly.
- Preserve public behavior deliberately. When retiring a feature, update its implementation, schema, examples, tests, and compatibility policy together.

## 6. Give each fact one authority

- Decide which record owns each fact. Treat summaries, indexes, and step projections as derived data with a repair/rebuild strategy.
- Route task-owned data through the task persistence boundary. Keep filesystem paths and storage layout out of the executor.
- Define a logical transition and its commit/recovery behavior before spreading it across files. Atomic replacement of one file is not a transaction across several files.
- Version persisted formats and validate relationships as well as individual fields: item IDs, cursor, plan revision, and command records must agree.
- Keep substantial artifacts and command logs outside frequently rewritten state; store stable references and bounded summaries.

## 7. Design interruption and concurrency behavior with the feature

- For each external side effect, distinguish not started, started with unknown outcome, failed, and completed. Define what resume does in each case.
- Use stable operation identities where retries can duplicate external work. Do not claim exactly-once behavior or idempotency without an implementation that supports it.
- Locks must protect the whole read–modify–write decision. Define lock ordering and ensure cleanup cannot invalidate active holders or waiters.
- Model multi-task coordination as recoverable operations with reconciliation. A lock alone does not repair a process that dies between writes.
- Keep saved-state inspection available without running handlers or importing unrelated plugin code.

## 8. Make storage adapters and extensions complete contracts

- Use a typed ABC or `Protocol` according to the need; neither is mandatory everywhere. Required operations must not masquerade as optional runtime stubs.
- Specify errors, missing-record behavior, overwrite/deletion semantics, and coordination guarantees. Verify the same observable behavior across implementations.
- Keep plugin APIs small: explicit context, declared inputs/outputs, validated results, and access only to the services they need by contract.
- Isolate third-party failures at the call boundary and preserve useful diagnostics. In-process plugins are trusted code, not sandboxed by namespacing.
- Avoid loading unused integrations for unrelated operations. Record compatibility and execution identity when resumption depends on plugin versions/settings.

## 9. Write ordinary, readable Python

- Follow `pyproject.toml`, the supported Python version, and local naming/import conventions. Use the project's formatter and linter.
- Prefer explicit control flow, comprehensible comprehensions, standard-library tools, and context managers. Avoid clever reflection when ordinary attribute access works.
- Use `None` deliberately for absence. Do not silently coerce malformed persisted booleans, numbers, or records into plausible defaults.
- Keep comments focused on constraints and reasons. Remove obsolete comments and empty branches. Do not turn historical implementation plans into descriptions of current behavior.
- Add dependencies when their concrete benefit exceeds the maintenance cost. Do not build a large custom framework merely to avoid a well-suited dependency.

## 10. Handle failures and command data precisely

- Catch exceptions at the boundary that can explain or recover from them. Preserve causes with `raise ... from error` where useful.
- Retry only classified retryable errors. Do not hide corruption or infrastructure failures as empty results or successful defaults.
- Use assertions for internal invariants, not validation of user input or persisted records.
- Pass commands as argument lists with `shell=False` by default. Explicit shell scripts must receive supplied data through a deliberate argument/environment contract, not raw interpolation into shell source.
- Define timeout/cancellation and output limits for external execution where needed. Bound persisted diagnostics without losing the reference to full output.

## 11. Verify behavior in proportion to risk

- For a bug fix, reproduce the observable failure and add a regression test when it protects meaningful behavior. Use integration tests for important wiring and contract tests for interchangeable storage adapters.
- Exercise interrupted writes, retries, and concurrency when changing persistence or execution. Use controlled synchronization rather than timing-dependent sleeps in concurrency tests.
- Test the supported API, not just private helpers. Do not let storage-adapter-specific expectations excuse differing contract semantics.
- Avoid tests that merely duplicate constants, getters, or implementation structure. Test agent-facing text when its meaning is a product requirement, and verify it reaches the real output path.
- Run relevant tests and lint/format checks. Report existing failures separately; do not repeatedly broaden testing without a reason. Measure performance before introducing speculative caches or concurrency.

## 12. Finish the change coherently

- Update affected user docs, examples, agent instructions, and compatibility notes when behavior changes. Do not claim guarantees stronger than the implementation/tests support.
- Remove superseded paths within the authorized scope once consumers are accounted for. Keep necessary migrations explicit and isolated.
- Report what changed, why, what was verified, and any remaining limitation. Distinguish observed results from assumptions and proposed future work.

## 13. Keep persisted data and extension APIs compatible

See [compatibility.md](compatibility.md) for the whole policy.

- Never bump a persisted format's schema version, `EXTENSION_API_VERSION`, or an extension's major version for an additive change: a new field, key, or option whose default keeps the old behaviour.
- A break is what does need a version: a removed or renamed field, a changed type or meaning, a removed value of a closed set (`Literal`, enum), or a changed default. Defaults are omitted on disk, so a new default silently changes every file written before it.
- Give a new persisted field a dataclass default and read it with `data.get(name, default)`; never list a defaulted field as required, so older files keep loading. Keep genuine identity fields required.
- Raise a schema version only in `ww.migrations.data`, together with the pure `dict -> dict` migration from the previous version and a new `tests/fixtures/formats/<format>/v<N>.json` sample. Never edit or delete an existing sample while its version is readable.
- Keep old shapes in `ww.migrations` only. Decoders and the engine handle the current shape; they never branch on a version.
- Change a configuration key's shape (`ww.yaml`, rule frontmatter, `ww.json`) by appending a `ConfigMigration` to `ww.migrations.config.catalog.CONFIG_MIGRATIONS`, with its guide `title`, `notes` and `example`, then run `scripts/generate_upgrade_guide.py` to regenerate `deprecations.md` (never edit that file by hand); the parser and models accept only the new shape. An extension declares its own in `Extension.settings_migrations`.
- Keep `written_by` out of every digest and comparison.

## Quick review before handing off

- Is this the simplest design that satisfies the current requirement?
- Does each rule have one clear owner, and are layer boundaries intact?
- Are configuration, persisted data, and public interfaces truthful and validated?
- Does every file an earlier ww wrote still load, and does every version change come with its migration and sample?
- Can an interrupted operation resume without skipping or duplicating unaccounted work?
- Do tests protect the changed behavior through the relevant public boundary?
- Have obsolete code and misleading documentation been addressed within scope?

Readability and project consistency are the baseline, not rigid ceremony. See [PEP 8](https://peps.python.org/pep-0008/) and [PEP 20](https://peps.python.org/pep-0020/). These project rules do not require rewriting working code merely to satisfy a stylistic preference.
