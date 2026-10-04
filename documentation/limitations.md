# Limitations

ww is in beta. This page collects what it deliberately does not do, and the
boundaries to expect before 1.0, in one place, so nothing here is a surprise
discovered mid-task. [CHANGELOG.md](../CHANGELOG.md) records what changed;
this page records what has not changed yet.

## Platforms

ww supports Python 3.10 and newer on POSIX systems, including current Linux
and macOS releases. Its filesystem locking uses POSIX `fcntl`; native Windows
is not supported. Windows users can run ww in a POSIX-compatible environment
such as WSL.

## Distribution and versioning

There is no published package yet, and no tagged releases to pin to. ww is
installed from a checkout in editable mode, and `main` moves frequently; see
the install section of [README.md](../README.md) for what that means in
practice.

**There is no formal compatibility guarantee yet**, because there are no
releases to define one against: nothing is contractually promised to survive a
pull of `main`, and there is no deprecation period before a change lands.

In practice the surfaces have settled. `ww.yaml` and the command line
have been stable for a while, and most work is internal refactoring, new
capabilities, and fixes. Incompatible changes are possible but uncommon; when
one lands it is deliberate and marked "Breaking:" in the changelog.

Every persisted format — task state, saved plans, execution records, the rule
stores — carries a schema version, and this build reads only its own version,
rejecting every other one rather than guessing or migrating it. The one
exception is saved plans written before `items` pass identity (schema 1), which
still load. When the
changelog says a schema changed, finish or reset in-flight tasks before
upgrading.

Once releases exist, the policy becomes the usual one. The package version
will identify a release rather than promising that every internal
representation is a public API. Before 1.0, a minor-version increase may
include an intentionally documented incompatible change. Patch releases will
not intentionally change documented command-line, persisted-record, or public
extension API contracts, and release notes will call out an incompatible
change and the affected boundary.

## Private runtime data

Task state and saved plans are private runtime data, not a general-purpose
interchange format. Filesystem layouts, `.ww/` contents, and task records are
implementation details; do not edit them by hand or consume them as a stable
external API.

`.ww/` may contain credentials or other sensitive values supplied to a
workflow. The audit file `.ww/executions.jsonl` records redacted invocations
and a stable error code, not arbitrary error text. It is an audit aid, not a
general secret-scrubbing system. See "Sensitive runtime data and support" in
[README.md](../README.md) before sharing any of it.

The documented extension API is the compatibility boundary for third-party
extensions, which should declare and test the ww release range they use. An
extension API change is announced in release notes, and removed public
extension API is deprecated for at least one minor release when practical.
Internal modules and bundled extensions are not a compatibility promise for
external code.

## Execution model

ww runs locally, on one machine, against one checkout at a time — plus the
optional `projects` list, which still runs from the same ww instance. It is
not a server or a scheduler. Cross-host locking, distributed scheduling, and
automatic reconciliation with arbitrary remote systems are intended future
capabilities rather than current behaviour.

There are no exactly-once external side effects. Interruption after automatic
work starts is represented as an unknown outcome and is never replayed
implicitly; recovery is explicit, through retry, a checker, or an operator
force. An extension without a checker may require an operator decision, and an
extension that writes outside its provided store is responsible for its own
concurrency.

Each task is guarded by one exclusive lock held across a whole `start`, `next`,
`complete`, or `reset`. Reads are deliberately unlocked, so `status` and
`instruction` stay usable while a command runs. Handoff order between waiters
is unspecified, and `WW_LOCK_TIMEOUT` bounds every wait so real contention
fails loudly rather than hanging.

## Storage

ww's authoritative state is the local filesystem under `.ww/` and nothing
else. Task state, artifacts, metadata, locks, and extension state all live
there; there is no interchangeable external storage provider, and the CLI
always uses the filesystem one.

## Workflow modelling

- A workflow may contain at most one `items` step, at any nesting level.
  Collected items belong to the workflow run, and ww expands every per-item
  stage in one place when collection completes. An `items` step cannot also
  declare `steps`, `loop`, an item operation marker, or child tasks.
- Each started run executes from its saved plan snapshot. An edit to
  `ww.yaml` reaches a running task only when the operator takes it at the
  `plan_changed` stop (`next --replan`); it cannot reach per-item or
  per-child stages the run has already expanded, or rewind past a children
  step whose child tasks exist. For those, reset the task and start it
  again.
- A loop has a round limit (`max_rounds`). Reaching it escalates rather than
  failing silently, and leaving the loop requires an explicit
  `next --force --reason "<reason>"`.
- The operator page is a local page served only for as long as a wait is in
  progress. There is no persistent web UI, no multi-user access control, and
  no remote operator.

## Agent limitations

Some boundaries come from the agent, not from ww. The detail behind the hook
gaps below lives in [agent-hooks.md](agent-hooks.md); this section only
collects the limitations, so it is not repeated there.

- An interactive step is always performed by the session that holds the
  conversation with the operator — the manager in the `auto` runtime. A
  delegated worker is a subagent and cannot talk to the operator, so an
  interactive step's `profile`, `agent`, `model`, and `reasoning` are ignored.
- Nested subagents: a delegated worker is itself a subagent. Whether it can
  spawn further subagents of its own varies by agent — Claude Code, for
  one, may not let it — and ww does not verify or enforce this; check the
  agent's own documentation before relying on nested delegation.
  `subagents: false` is likewise only an instruction on the step's page
  today, not a hard block: ww has no pre-spawn hook yet.
- Per-agent hook gaps: Antigravity has no session-start event and never
  reports a worker's own stop, so its manager is never reminded about a
  delegated step; Codex only loads project hooks once `.codex/` is
  trusted; only Claude Code keeps a hooks file out of version control. See
  the per-agent table in
  [agent-hooks.md](agent-hooks.md#agents-and-their-files).
- Recovering a conversation: an interactive step is recorded once, when the
  conversation ends. When a session ends before that, the `interrupt` hook
  recovers the exchange from the session's transcript for Claude Code and
  Codex only, and depends on their internal transcript formats, which may
  change with any release, so recovery is best effort. For other agents the
  conversation is not recorded, and a crash or a closed tab runs no hook at
  all.
- Question-tool availability and supported fields vary by host session. ww
  instructs agents to inspect the available schema, use structured options
  when offered, and use a text-only question only when the tool requires it.
  When an asynchronous answer is pending, the workflow waits for the
  operator's explicit answer; a timeout, dismissal, or preselected value does
  not complete the choice.
- Editing its own workflow files: some agents' safety layers, such as Claude
  Code's auto mode, refuse an agent's edits to the workflow files it runs
  under (`ww.yaml` and `.json`) as self-modification, even
  after the user agreed in chat. The agent then prepares the change as a
  script or patch, and the operator applies it.

## Reporting a gap

If something here blocks real work, open an issue — the reporting guidance in
[README.md](../README.md) says what to include.
