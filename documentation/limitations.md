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

ww is published to PyPI as `ww-agentic-workflows`, as public betas,
`1.0.0b1` and on, built from `main`. There is no stable release yet; pinning
means choosing a beta version, or a commit in a source checkout. See the
install section of [README.md](../README.md) and
[releases and development builds](development-releases.md).

**There is no formal compatibility guarantee yet**, because there are no
stable releases to define one against: nothing is contractually promised to
survive an upgrade to the next release or a pull of `dev`, and no
deprecation period is promised before a change lands.

In practice the surfaces have settled. `ww.yaml` and the command line
have been stable for a while, and most work is internal refactoring, new
capabilities, and fixes. Incompatible changes are possible but uncommon; when
one lands it is deliberate and marked "Breaking:" in the changelog.

Every persisted format — task state, saved plans, execution records, the rule
stores, feedback and run reports — carries a schema version and the ww
version that wrote it. A schema version changes only on a
backward-incompatible change, and each change ships with a migration: this
build reads every version of a format from the oldest it still migrates up
to its own, upgrading an older file when it loads it. It refuses a file a
newer ww wrote, naming that ww and asking you to upgrade, and it refuses a
file older than any it still migrates, naming the ww to finish or reset that
task with. A downgrade is not supported: an older ww may refuse a file a
newer one wrote even at the same schema version. See
[compatibility.md](compatibility.md).

Configuration that changes shape ships with a configuration migration where
ww can describe the change: ww keeps reading the old shape, migrating it in
memory as each file loads, warns with the file and line, and `ww lint` lists
what is left; once the old shape is removed, it is an error naming the
removal. A configuration snapshot a run pinned is migrated the same way and
never rewritten. See [deprecations.md](deprecations.md).

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

## Configuration authority

A task never reads configuration from its git worktree. Its worktree's
`ww.yaml`, `ww.json`, or imports do not take effect, for itself or for the
child tasks it launches; a copy that differs is only flagged on the task's
instruction pages. In a Git project tasks read the configuration committed on
`config_ref` (the ww/git default base branch unless set), pinned to the
commit at which each run started: commit a configuration change there, then
run `ww config refresh <task>` for running tasks. Uncommitted edits in the
primary checkout reach only the commands that edit the configuration, and
the untracked `ww.local.*` files. Checks cannot use untracked tooling such
as `node_modules` or a virtualenv through `WW_ROOT`; they find tools on
`PATH` or in the step's directory. Without Git, configuration is read from
the primary checkout.

## Storage

ww's authoritative state is the local filesystem under `.ww/` and nothing
else. Task state, artifacts, metadata, locks, and extension state all live
there; there is no interchangeable external storage provider, and the CLI
always uses the filesystem one.

## Workflow modelling

- Each `items` container owns its own collection; nested collections are independent.
  An outer step chooses one container kind: `steps`, `items`, or `children`.
- Each started run executes from its saved plan snapshot. An edit to `ww.yaml`
  reaches a running task only when the operator takes it at the `plan_changed`
  stop (`next --replan`); it cannot reach per-child stages the run
  already expanded, or rerun or rewind past a children step whose child
  tasks exist.
  For those, reset the task and start it again.
- The operator page is a local page served only for as long as a wait is in
  progress. There is no persistent web UI, multi-user access control, or
  remote operator.

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
- Per-agent hook gaps: Codex only loads project hooks once `.codex/` is
  trusted; only Claude Code keeps a hooks file out of version control. See
  the per-agent table in
  [agent-hooks.md](agent-hooks.md#agents-and-their-files).
- Recovering a conversation: an interactive step is recorded once, when the
  conversation ends. When a session ends before that, the conversation is not
  recorded and ww does not recover it from the session's transcript; the next
  session asks the operator where they were.
- Question-tool availability and supported fields vary by host session. ww
  instructs agents to inspect the available schema, use structured options
  when offered, and use a text-only question only when the tool requires it.
  When an asynchronous answer is pending, the workflow waits for the
  operator's explicit answer; a timeout, dismissal, or preselected value does
  not complete the choice.
- Headless agents: ww starts only Claude Code, Codex and Cursor headless,
  through the command-line flags of the versions it was tested with (Claude
  Code 2.1, Codex 0.164, Cursor 2026.06); a newer program is run with a
  warning, since its flags may have changed. A headless agent cannot talk to
  the operator or ask for permissions: what it may do is decided up front by
  `permissions`, and Codex and Cursor offer no `read-only` level that still
  lets the worker record its result. Cursor takes no reasoning setting; its
  model names carry it. Cursor's `status` can report a login that its
  headless mode then refuses, so ww only warns about it. Claude Code lists no
  models, so a model name other than its aliases and `claude-*` names is
  passed on unchecked. Runs are watched by a process ww leaves running; a
  queued run starts only while the manager waits. Process groups and signals
  make this POSIX-only, like the rest of ww.
- Editing its own workflow files: some agents' safety layers, such as Claude
  Code's auto mode, refuse an agent's edits to the workflow files it runs
  under (`ww.yaml` and `.json`) as self-modification, even
  after the user agreed in chat. The agent then prepares the change as a
  script or patch, and the operator applies it.

## Reporting a gap

If something here blocks real work, open an issue — the reporting guidance in
[README.md](../README.md) says what to include.
