# Architecture

## Purpose

`ww` makes workflow behavior inspectable and executable through one compiled
plan. A single compiler expands the selected workflow into every effective step
and lifecycle handler, in execution order. This prevents instructions from
silently omitting global, workflow, or step hooks.

## System shape

```text
notation frontend + project-local agent actions
                    |
                    v
 normalized definitions -> semantic validator -> flat WorkflowPlan -> task snapshot
                                                                  |          |
                                                                  v          +-> plan CLI rendering
                                                     lifecycle coordinator
                                                               |
                           +---------------------------+---------------------------+
                           v                           v                           v
                  transition functions        automatic action executor     instruction builder
```

The built-in frontend in `../src/ww/config/` parses `../workflows.yaml` into the
immutable definitions in `../src/ww/workflow_config.py`; another notation can
produce those definitions directly through the same loader contract. Shared
cross-definition rules live in `../src/ww/workflow_validation.py`, so notation
parsers do not acquire different workflow semantics. Validation is also where
the workflows ww provides to every project, `catchall` in
`../src/ww/core_workflows.py`, join the configured ones, unless the project
defines a workflow of the same name or switches it off in
`../agentic-workflows.json`; every loader passes through it, so no frontend can
miss them. `lookup` (`../src/ww/cli/lookup.py`) is the catch-all's entry
point: `../src/ww/task_references.py` maps what the operator called a task onto
the task format and the IDs the storage port lists, and the command answers
with one next step, asking the operator through the agent's choice menu
(`../src/ww/agents.py`) before a new task is created. `inherit` is resolved by
the YAML frontend itself, once every workflow is parsed: the heir becomes a
complete copy with its own settings on top, and global hooks filtered to a
workflow are extended to its heirs, so validation, the compiler, `plan`, and
`discover` only ever see complete definitions. Core workflows join only at
validation, after that, so they cannot be inherited. The service receives its
configuration loader at its composition boundary and does not know which
notation produced the model. The `WorkflowPlanCompiler` in `../src/ww/plan/`
validates that model again at its public boundary, then exclusively resolves
implicit action names, filters hooks, evaluates interpolation availability, and assigns
deterministic execution order.

Before the YAML frontend parses anything, `../src/ww/config/composition.py`
composes `../workflows.yaml` and the files its leading `imports` list names
into one mapping and dumps it back to YAML text. The parser receives that text
exactly as it would a single file, so composition adds no parsing rules of its
own: it only folds files in import order with the root last, replacing
same-named catalog and profile entries in place, appending hooks per phase,
and letting later scalar keys win. It keeps a record of each override, which
`lint` prints as notices, and leaves a name repeated within one file untouched
so validation still reports it. A root file without `imports` passes through
unchanged. Composition runs on every read and writes nothing to disk. The other
readers of the raw file, `init`'s checks in `../src/ww/cli/initialization.py`
and the storage's missing-key and setup checks in `../src/ww/storage.py`, read
the composed mapping too, so a definition in an imported file counts as
present everywhere.

Explicit `argv` and `shell` action fields are the canonical YAML command
notation. `CommandAction` rejects string commands and emits typed
`CommandDefinition` values. The normalized model and compiler do not carry or
reinterpret YAML command grammar; another frontend must emit the same typed
actions directly.

Root `task_format` is part of that normalized authored contract. It accepts
only the explicit `{timestamp}`, `{digit}`, and `{uuid}` placeholders and drives generated
top-level and child task IDs; when it is absent, ww retains the standard
`TASK-<timestamp>` convention. Numeric formats allocate the first free positive
integer without a fixed ceiling; timestamp collisions use a bounded suffix retry.
When `ww/git` worktrees are enabled, generated IDs also reserve the configured
rendered worktree path. This prevents a task whose persisted state is gone from
adopting an unrelated or stale checkout with the same task-derived name.
Project configuration has a narrower purpose: `../agentic-workflows.json` contains
ww-wide operational limits, per-extension settings, and optional execution
settings for the implicit init and workflow-summary built-ins. The default loop
limit is materialized during initialization because it is a user-facing safety
boundary; built-in model defaults stay internal because they do not need
authored configuration merely to reproduce default behavior. Its `enabled`
switch is the one setting agents act on directly: when it is false, `discover`
tells agents not to use ww and `start` refuses, while commands for existing
tasks keep working so in-flight work can still be inspected or finished.

Initialization is a convergent project-repair operation rather than a one-time
state transition. It fills absent root configuration keys and recreates missing
ww-owned files, but preserves authored values. Runtime state, including task
documents, lives below `../.ww` so projects can exclude one directory as a unit;
`init` migrates the former top-level `../tasks` directory when there is no
conflicting destination. Agent instructions remain at the project root because
`../AGENTS.md` and `../CLAUDE.md` must be able to reference a durable, versioned file.

That file is deliberately short. It states when to use ww and the rules for
following its responses, and it sends agents to `discover`
(`../src/ww/cli/discover.py`), which renders the project's current workflows,
modes, runtimes, roles, start options, and commands. Keeping the choices in a
command rather than in copied prose means the guidance cannot drift from the
configuration. Branch strategies are the one option core cannot know itself:
an extension that names branches declares `branch_strategies`, and the registry
asks only configured extensions, as it does for reserved paths. The shipped
`ww` skill is a thin trigger for the same flow, and `noww` its opt-out; `init`
installs both into each agent directory the discovery module knows about.
Potentially project-opinionated edits such as `../.gitignore` remain explicit user
choices.

This separation is intentional. Configuration is an authored contract and a
`WorkflowPlan` is its agent-specific interpretation. At `start`, ww persists
that interpretation in the authoritative task state document. State transitions
use only the saved snapshot. No executor
or instruction layer reimplements hook selection, handler discovery, or YAML
interpretation.

`WorkflowService` is the lifecycle coordinator, not the implementation home
for every concern it invokes. Named state changes—beginning, completing,
failing, retrying, recovering, selecting assessment outcomes, and
materializing items—and their matching step projections live in
`../src/ww/transitions.py`; subprocess and extension side effects are invoked
by the registered actions through `ActionExecutor` in
`../src/ww/action_execution.py`; and caller-facing state views including
bootstrap requests are built in `../src/ww/instructions.py`. Structural
assignment membership lives in `../src/ww/assignments.py`, keeping
manager/worker boundaries independent of presentation and handler names.
Three coordinators own the larger sub-lifecycles and reach the service only
through the `RunLifecycle` protocol in `../src/ww/run_coordination.py`
(load, commit, drain, resume, render): `../src/ww/recovery.py` resolves
interrupted automatic items, `../src/ww/child_coordination.py` starts children
and projects their run records onto the parent, and
`../src/ww/metadata_publication.py` validates, records, and projects metadata
intents. Task identity rules (validation, generated IDs, worktree claims) live
in `../src/ww/task_ids.py`. The service keeps
the ordering and locking responsibility that joins those pieces, while
`RunCoordinator` in `../src/ww/run_coordination.py` loads and atomically publishes
complete run records through the persistence ports. These are direct
collaborators rather than a general dependency-injection framework: the split
exists to make side effects and state rules independently checkable while
keeping the execution path explicit.

`../src/ww/output.py` owns CLI presentation, including compiled plan rendering and
selection of the Markdown or JSON instruction adapter. Keeping plan rendering
outside the compiler leaves compilation focused on producing normalized plan
data, while the small adapters remain a deliberate format boundary rather than
a general response framework. There is no project-local template lookup or
parallel artifact-rendering subsystem; artifacts are the content supplied when
an artifact-enabled item completes.

For an active workflow step, the instruction builder also exposes the ordered
names of its later sibling steps. Markdown presents those names as lightweight
scope guidance so an agent can avoid duplicating work reserved for later steps,
without copying their descriptions into the current prompt. Deriving this list
from the persisted plan keeps the guidance deterministic across status and
resume operations.

Per-item sections are the one deliberately deferred part of compilation. Their
compiled templates remain immutable in the snapshot. Once collection finishes,
ww publishes a new, numbered plan revision that contains one concrete lifecycle
for each collected item while retaining the original template plan for audit and
recovery. Initial and materialized items use the same execution-record factory,
so automatic command ledgers cannot disappear during expansion. Plan items also
carry their ordered ancestor paths explicitly; materialization substitutes the
item path throughout that relationship and rebuilds the recursive step
projection. The executor therefore never has to infer hierarchy from display
paths.

`depends_on` is deliberately an artifact-reference hint, not a scheduler. The
semantic validator requires it to name an earlier artifact-producing sibling,
the compiler retains the reference in the immutable plan, and the instruction
builder makes that artifact an explicit input even when the step supplies its
own prompt. Execution order remains the authored step order.

Assessments add a deliberately bounded form of conditional topology. The
compiler retains every declared outcome as a labeled nested subtree in the
immutable plan, rather than constructing a different plan after the agent has
made its judgment. Once the assessment completes, the manager selects one
declared label through `next --outcome`; execution activates only that subtree
and records all alternatives as skipped. This makes the decision explicit and
auditable while preserving normal substep lifecycles, artifacts, and recovery.
The compact assessment has the same boundary without a subtree: a positive
outcome continues normally and a negative outcome completes the workflow. A
declared outcome with `stop_workflow: true` compiles to no subtree at all; the
assessment item lists it among its outcomes and its stops, and selecting it
completes the workflow the way compact `negative` does.
`../src/ww/assessments.py` is the single reading of that plan data: which
outcomes exist, what each does, and whether a completed assessment is waiting
for one. It also supplies the standard answers an assessment does not declare,
which select no subtree: the transition skips every outcome's work and moves
the cursor past it. The transition that applies an outcome and the instruction pages that
ask for it both use it, so the page never offers a command the transition
refuses.

## Handlers and discovery

`HandlerDefinition` holds a typed `DefinedAction` or a core handoff definition,
plus shared defaults such as
execution settings, `provide`, and `update_metadata`. A `StepDefinition` adds
container and step policy. `HookDefinition` adds filters, lifecycle phase, and
provenance while storing the handler directly. Named hook references
and name-only extension steps are represented as name-only handlers and
resolved during compilation. This keeps extension actions on the same compiled
execution path whether they define a lifecycle boundary or an authored step. A
step's YAML `handler` reference instead copies its root handler definition while
retaining the step's own identity and explicit overrides, so it compiles as the
same single ordinary step rather than introducing a reference action at runtime.
Catalog entries may carry a complete step container as well as an action; this
lets workflows reuse a named loop or nested sequence without a second execution
or container abstraction. The parser materializes that tree at the referencing
step, leaving validation and plan compilation responsible for the same topology
regardless of whether it was inline or shared.
A compiled `PlanItem` stores one operation. Ordinary work uses `PlannedAction`
with a stable registry identifier and action-specific payload; the closed core
set uses `LoopBoundary`, `WorkflowHandoff`, and `ChildWorkflowRun`. The owner
and execution mode remain separate core policy:

- CLI commands, loop boundaries, and workflow transitions are `ww`-owned.
- Skills, slash commands, prompts, and MCP prompts are agent-owned.

Plan items additionally carry an execution mode. CLI and extension items are automatic:
they remain ww-owned even if their declared inputs must first be collected from
an agent. Agent-facing inputs are not independent work items; the completion
instruction collects them and resumes the automatic command.
Skills, slash commands, prompts, and MCP prompts are explicit agent
instructions. An MCP prompt records the connection name and renders an
instruction telling the agent to use that connection before the configured
prompt text. Because ww does not execute agent-owned MCP calls itself, MCP
failures must be reported by the agent as functional failures. That preserves a
single task state machine where `next` retries a failed item. Workflow
transitions are a coordinator operation, separate from automatic command
execution. Loop entry and repeat items are another coordinator boundary, while
the stop condition belongs to an agent-owned body step. This keeps the judgment
with the worker that has the relevant result and makes loop exit a deterministic
command rather than a conversational manager decision.

Skipping a failed item is an exceptional operator action rather than an agent
retry policy. The CLI `next --force` path therefore asks the service what the
force would do first, refusing without a prompt when nothing is forceable, and
then requires an interactive confirmation that states that effect and
explicitly warns agents not to approve it without permission;
a negative or unavailable response leaves the failed state unchanged. This
preserves the normal retry behavior while allowing an operator to proceed after
resolving an issue outside ww.

Automatic commands preserve the boundary between executable source and
workflow data. Normal actions are stored as argument vectors; the compiler and
executor interpolate each argument independently and invoke the program without
a shell. Shell behavior is a separate, explicit action whose script is never
interpolated. It can receive dynamic values only through declared environment
variables or positional arguments. This representation is persisted in the
plan snapshot so a resumed run cannot reinterpret ordinary data as shell
syntax.

The YAML frontend mirrors this model: `argv`, `shell`, skill, slash-command,
MCP, and prompt action keys live directly on global handlers, steps, and
singular hooks. Only an ordered group uses the `handlers` container.

`AgentDiscovery` resolves actions from `.agents/` plus the selected agent’s
directory. For an action without an explicit kind, automatic resolution is
one-way and singular: skill, then slash command, then prompt fallback. CLI
actions are already typed before discovery begins. This removes the prior
possibility of returning both a skill and a command for one configured action.

`../src/ww/actions/contracts.py` defines the typed payloads, phase contexts,
and internal registry. Each built-in implementation has its own module in
`../src/ww/actions/`, and `__init__.py` registers them. Parsing chooses one
action at the notation boundary. Each
registered action validates its definition, plans its typed payload, encodes and
decodes plain payload data, and produces structured instruction content. The
compiler supplies interpolation and discovery through `ResolutionContext` while
it retains item identity, ordering, hooks, dependencies, and shared policy. The
instruction builder supplies state, role, runtime, and requested and effective
execution settings through `InstructionContext`; it then adds completion values,
metadata, artifacts, and continuation commands around the action's content.
Loop boundary and child-progress wording are core presentation helpers, as are
their state transitions. `../src/ww/control.py` directly inspects the typed
core operations for instructions, assignments, and lifecycle transitions; core
keeps locks, run creation, child links, cursor updates, and reusable loop-state
records. A persisted plan item stores its operation under `operation` with an
explicit `type`: `action` for registered actions (with their `identifier` and
`payload`), or `loop`, `workflow_transition`, and `child_workflow` for core
controls, whose fields are owned by `../src/ww/operations.py`. In YAML the core
controls have their own keys, `workflow`, `workflow_per_child`, and `loop`; the
explicit `action: {type: ...}` form selects registered actions only and rejects
a core control's type, so engine behaviour is never spelled as an action. This
does not introduce a public YAML plugin format or new scheduling primitives.

Handler references retain shared precedence in the parser, while the action's
`override_definition` hook owns payload-specific inheritance.
Commands use it for permissions and prompts for text; aliases that share a
payload contract follow the same behavior. Preflight receives read-only
operation data and identity validation only. Recovery receives saved-operation
data and checker-only extension access, separate from the command executor used
for real execution; neither phase needs a fabricated plan snapshot.

Automatic actions additionally implement the optional typed execution contract:
they decide which effects to request and interpret their aggregate result, but
return only a typed result (success or failure, output, declared values, and an
optional workspace proposal). They never receive a plan snapshot, execution
state, persistence adapter, or lifecycle callback. Core validates that result,
records completion or failure, applies permitted values/workspace changes, and
advances the cursor. This keeps a new automatic action from acquiring an
accidental way to change workflow lifecycle policy.

Composite definition expansion has a separate internal planning-contract
boundary in `../src/ww/plan/constructs.py`. The compiler normalizes one
`StepDefinition` into a typed sequence, assessment, item-flow, loop,
child-workflow, or leaf input and dispatches it through a checked registry.
Construct planners request only nested logical compilation, a scoped annotated
region, a leaf emission, or a typed loop boundary; they never receive the
compiler, mutable plan list, execution state, or persistence services. This
keeps constructs responsible for their own topology while the compiler remains
the single owner of lifecycle hooks, inherited policy, identities, positions,
and the persisted flat `WorkflowPlan`. Assessment branch annotations and
item/child collection annotations are immutable planning data, preventing a
nested scope from leaking into later siblings. The registry is internal, not a
new YAML plugin surface: a new built-in construct needs a normalized input,
planner, registration, and a test that demonstrates shared lifecycle behavior.
Runtime loop transitions, child coordination, and handoff execution deliberately
remain consumers of the saved plan rather than construct-planner concerns.

An automatic action may override the `preflight` hook. Core runs it before
recording the durable `in_progress` boundary; for extensions this verifies the
frozen provider identity before a handler can run. A rejected identity is thus
a configuration failure, not a synthesized unknown external outcome.

The execution context exposes read-only runtime identity and values plus narrow
effect services. Its command service executes exactly one declared segment and
owns durable start/result records, operation IDs, attempts, environment
injection, full output storage, and resume reads. `CommandAction` owns segment
ordering, interpolation, aggregation, and assertions; it asks the service for
already-completed output before rendering a segment, so recovery never
reinterprets completed work. The extension service preserves frozen provider
identity and settings while giving `ExtensionAction` the established extension
API context. Recovery checking is the `check_recovery` hook on
`AutomaticAction`, not a built-in action name, so any registered automatic
action can reuse the recovery coordinator without new lifecycle branches.

Recovery checks return an internal generic action result rather than an
extension-shaped value. A checker explicitly attests either the whole action or
one interrupted command segment. Whole-action attestation applies its validated
output, declared values, and proposed working directory through the normal
result path. Segment attestation records only that segment's stdout and resumes
the remainder; it cannot introduce values or a workspace change. `ExtensionAction`
adapts the public extension checker API at this boundary, preserving extension
compatibility without making every automatic action extension-shaped.

The registry is an internal extension point. To add an agent-owned action,
define separate immutable definition and planned payload types if they differ,
subclass `Action` (or `AutomaticAction` for work ww runs itself) and implement
its abstract lifecycle methods with a stable identifier and planned type.
Register it with `actions.register(...)` before loading configuration. A YAML
step or handler can select it with `action: {type: <identifier>, ...}`; built-in
shorthand retains its current syntax. Generic parsing, planning, snapshot, and
instruction consumers need no new action-kind branch. New automatic scheduling
semantics require a corresponding core capability and are outside this internal
registration boundary.

Registration rejects inconsistent actions before planning: the class must
subclass `Action`, owner and execution mode must agree, and automatic actions
must subclass `AutomaticAction`; the abstract base enforces the lifecycle
methods. Everything else core needs to know about a payload—durable command
segments, output attestation, what an operator may attest manually, artifact
attribution, extension binding—comes from one
`traits(planned)` call returning an `ActionTraits` value with plain defaults,
so core keeps the policy and actions only state facts about themselves.
Explicit action YAML can declare `outputs`; core still validates and
persists them, so a custom automatic action can feed downstream values without
new parser or executor branches.

Profiles are resolved by the compiler only for workflow and step actions; they
tailor an agent's instructions without defining execution topology. A step
profile overrides the workflow default. The compiler prefers a matching profile
file in the selected agent directory's `agents/` folder, then inline/root YAML
text, and finally the profile name alone. The compiled plan stores the resolved
instruction, so it remains stable for the life of a task.

Every compiled workflow starts with ww's reserved `init` step, even though that
step is absent from the normalized workflow definition. Keeping it compiler-owned
gives YAML and future notation frontends one fixed requirement-capture boundary:
the agent corrects grammar and style, preserves meaning, and writes the task's
requirements artifact without analysis or planning. The step uses low reasoning
and deliberately ignores workflow profiles so workflow-specific behavior cannot
change this archival purpose. Configuration validation rejects any declared
step named `init`, but includes the implicit step when validating hook filters
and top-level artifact dependencies.

## Lifecycle ordering

The compiler emits workflow and step lifecycles in one ordered plan. Global and
workflow `before_start_workflow` hooks run once before the implicit `init` step.
At `start`, the manager immediately records the supplied `--init-artifact` in
the implicit step; it is never dispatched as an agent assignment. Each declared
step then runs `before_in_progress`, the step itself, `before_complete`, and
`after_complete` in global, workflow, then step order. After the final step,
global and workflow `before_complete_workflow` hooks run once, followed by the
built-in summary or handoff transition. Workflow boundaries reject step filters
because their purpose is to provide one stable location independent of workflow
shape. Filters remove an item from the plan entirely. Because `init` is an
ordinary artifact-producing step in the compiled plan, a first
declared step can consume its artifact with `depends_on: init`.

The compiler carries declared values forward after both steps and hooks,
validates `{{name}}` interpolation data flow, binds available built-ins, and
preserves unresolved declared inputs for future execution. A plan is therefore
safe to render before a task exists.

Core variable names and value construction are owned by `../src/ww/variables.py`.
Stable values such as `__task_id` and `__workflows` are bound during compilation;
dynamic values are resolved from current execution state when an instruction or
automatic action is rendered. This lets `__task_workspace_dir` follow a run's
selected checkout without freezing a path into the plan snapshot.

Durable metadata is separate from the workflow-value ledger. Agent-owned
handlers declare captures with `update_metadata`; the compiled item retains each
agent-facing completion name, dotted storage path, scope, and description so a
resumed run produces the same instruction. Task-scoped references use
`{{metadata.<path>}}`, while project-scoped references use
`{{project_metadata.<path>}}`. Both remain unresolved at compile time because a
value may be created by an earlier item or another task. Explicit namespaces
keep ownership visible and avoid implicit task-over-project shadowing.

## Execution and persistence

`ww lint` and `ww plan --workflow <name> --agent <agent>` remain read-only.
Linting validates the complete normalized configuration without requiring a
particular workflow or agent, so configuration feedback does not depend on
plan-rendering inputs or create runtime audit/state data. `start` publishes one
authoritative task state document and drains leading ww-owned automation.
A manager-role `next` dispatches one structural assignment. For an ordinary
leaf, the assignment contains its preparation hooks, main action, and completion
hooks. Worker-role completion stores each agent result and advances through
eligible automatic and agent-owned items inside that bound. When the bound ends,
the durable assignment marker is cleared and control returns to the manager
before the next lifecycle starts.

`../src/ww/assignments.py` is the central structural rule. It derives membership
from step paths, ancestors, lifecycle phases, coordinator kinds, and the final
summary marker. Execution state persists only the assignment's first plan-item
identity and inherited model/reasoning. Recomputing the bound from the saved plan
keeps reload and dynamic materialization deterministic without storing fragile
cursor ranges. Parent-only preparation hooks are separate assignments; trailing
parent completion hooks follow the final descendant. Concrete dynamic items,
child coordination, workflow transitions, and successor execution instances
remain manager boundaries. The exceptions are an `items` step whose
`item_assignment` is `per_item` or `all_items`, and a loop body under
`per_iteration`: in the `auto` runtime the bound extends across later stages
of the same item, of every item, or of the same loop round, and grows its
lineage as it absorbs each stage so stage completion hooks stay inside. Both
share one rule for where a span ends early: a stage that resolves to another
agent, model, reasoning, or profile, or that is reserved for the manager with
`subagents: false`, starts a new assignment, because one running worker
cannot change those. The `single` runtime keeps per-step bounds. Each stage is still its own plan item
with its own record, so completion, reload, and recovery need no span-specific
state; only the instruction builder marks the first stage of a span with its
scope and later stages as compact continuations.

Declared `--metadata name=value` inputs are validated at the same completion
boundary and routed to the scope recorded in the saved plan. Completion first
commits the source item and its metadata publication intent in the authoritative
task aggregate; only then is metadata projected. A failed projection is retried
from that intent, so metadata never proves an uncommitted completion. Task
projection is serialized by the task lock. Project projection is keyed by the
producing operation and records the observed values for its keys: retry is
idempotent when the desired values are already present, while an intervening
different value is reported as a conflict rather than overwritten. Later items
are rendered or dispatched only after their task's pending projection succeeds.
`instruction` renders the detailed state, including the active
assignment or manager handoff and a live automatic operation as unfinished
rather than guessing that its process died. Roles describe caller responsibility,
not authorization, and omitted roles preserve the earlier service behavior.

The ledger records command segments separately, so a partial CLI failure retries
only the failed or unrun segment. Every finished segment is committed the moment
its process exits, success or failure, so the ledger is a durable boundary in
both directions: a success is never replayed to aggregate its output, and a
recorded failure stays a known outcome even if ww dies before the coordinator
writes the item failure. Each new run persists a unique execution-instance
identity in addition to its human-readable numbered run name. Item and command
operation IDs include that identity, so retries keep the same idempotency key while
resetting and recreating a task cannot recover an external effect from the deleted
execution. Older records retain their already-persisted operation IDs. Each command
also records its attempt count, and command actions receive those identities through
environment variables. Full stdout and stderr streams
live in stable, run-scoped output artifacts. Hot execution state keeps only a
bounded preview plus the artifact reference, which prevents one noisy command
from making every later transition rewrite its complete output. Recovery reads
the referenced stream when it must reconstruct output from completed segments;
legacy inline streams remain readable. The state also contains a recursive step
projection for human-readable parent/child status and a matching recursive
artifact layout. Parent steps begin with their first child and complete only
after their descendants and parent-scoped completion work finish.

Each compiled plan item carries the one-based declaration ordinal for every
level of its step hierarchy. Persisting this ordinal path keeps artifact names
ordered among siblings without asking storage adapters to reconstruct workflow
structure from a slash-separated name. The compiler assigns the values before
bootstrap filtering, and per-item expansion recalculates them when runtime
siblings are materialized, so artifact references remain stable representations
of the effective workflow hierarchy. Hook artifact filenames remain keyed by
their flat plan position because multiple hooks can share one step boundary.

Loop progress uses the same flat plan and cursor rather than a second executor.
The plan carries paired loop entry/repeat markers around normal nested plan
items; the execution state persists an iteration counter. Reaching the repeat
marker resets only the records inside those markers and derives a new
iteration-scoped operation identity, so automatic side effects from different
rounds cannot be mistaken for retries. Artifact storage uses those same durable
counters to insert an `iteration-NN` directory beneath each enclosing loop
wrapper. This keeps every round's results addressable and prevents a repeated
body step from overwriting its earlier artifact; nested loops naturally add one
directory at each loop boundary. Body items carry the enclosing loop's
identity and its `loop_assignment`, so `assignment_at` can keep consecutive
body steps of one round in one worker assignment when they resolve to the same
worker settings; the boundaries remain coordinators and always end an
assignment. The loop entry also represents the wrapper's
artifact contract. A successful explicit stop keeps the stopping body's
iteration artifact and records the same final result as the wrapper artifact
outside the iteration directories, giving later workflow work one stable
reference for the completed loop as a whole. The wrapper's `artifact` setting
can disable that aggregate reference independently of body-step artifacts.

The compiler also freezes each loop's effective maximum from its local override
or the project-wide default. Once the persisted counter reaches that maximum,
the repeat boundary remains pending and exposes no continuation; this makes the
safety stop durable and forces the manager to escalate the saved round results
to the user instead of allowing another worker dispatch. The same force that
skips a failed item is the operator's exit from that stop: `next --force` at an
exhausted repeat boundary records the reason on the boundary and advances past
the wrapper, so a capped loop is never a dead end. A break-enabled worker
records a durable exit intent through `ww loop --break`; the executor finishes
that step's normal completion hooks before skipping the rest of the body and
advancing to the wrapper's completion lifecycle.

Closed sets shared across plans, execution records, child coordination, and
instructions are expressed as `Literal` contracts in `../src/ww/contracts.py`.
`PlanItem` also validates combinations that would make automatic dispatch
ambiguous, and its assertion has the concrete normalized assertion type.
Persisted model loaders reject malformed nested records rather than filtering
them, because silently dropping part of an execution record would turn
corruption into changed behavior. Large cross-layer records use keyword-based
construction so schema evolution does not depend on positional field order.
Mypy checks the public models, ports, and extracted boundaries as a development
release gate alongside Ruff and pytest.

Automatic work is written as `in_progress` before an external side effect. A
locked `next` that finds that boundary settles it
(`settle_stale_automatic_item` in `../src/ww/transitions.py`): when a command
segment had already recorded its non-zero exit, the process demonstrably
finished and only ww's bookkeeping was cut short, so the item becomes a known
failure with that exit code and output and takes the ordinary failed-item
path; otherwise the item is `interrupted`, preserving completed segments and
making the outcome explicitly unknown. This matters because a process can die
after a remote or Git operation succeeds but before ww records completion.
`next` never replays such work implicitly. The one exception is the author's
own declaration: a command handler with `idempotent: true` states that a
second run cannot do damage, so `RecoveryCoordinator.replay_if_idempotent`
returns the interrupted and unrun segments to pending under the same
operation identity and `next` continues, with no operator decision. Without
it, `next --retry` offers explicit replay with the same operation identity,
or an operator attestation; CLI attestations can supply captured output for
assertion evaluation. A command that cannot be launched is a known failed
attempt: ww records the process-creation error and uses the ordinary
failed-item retry path because no external process acquired an unknown
outcome. A checker-capable automatic action can return succeeded,
not-succeeded, or unknown, allowing safe default recovery without claiming
exactly-once execution. Extension contexts carry the current plan and work-item identities,
attempt number, and stable operation identity. Successful extension results may
return only their handler's declared structured outputs; ww records those
outputs separately from human-readable output and adds them to the workflow
value environment. Recovery checkers follow the same validation and output
rules, so recovery and first execution have one result contract. Explicit
action-versus-segment scope prevents a generic checker success from being
mistaken for evidence that every side effect in a multi-segment action occurred.

`handoff: true` is the one supported cross-workflow operation. Its terminal
transition completes the selection workflow's own run and opens the successor as
the next numbered run, rather than replacing the plan in place. Both workflows
therefore keep their own snapshot, state, and artifacts, and the run ledger shows
how the task got from one to the other — the selection run's summary column, which
a handoff workflow never fills itself, records `handed off to <target>`. The
task document's handoff value remains the guard that keeps chained handoffs out.
General nested workflow calls are still outside the execution model; workflow-level
`workflows` keys are rejected by the YAML frontend rather than represented in
the normalized domain model.

`recommended_next_workflow` is the operator-confirmed counterpart of a handoff.
The compiler freezes it into the plan, so a finished run keeps offering the
same successor after the configuration changes, and the completed page asks the
operator through the agent's choice menu before showing the `start` command for
the same task. Nothing in core starts it: the agent does, with an ordinary
`start`, only on the operator's answer. Validation rejects a recommendation
that names no workflow or that sits on a handoff workflow, whose successor is
already fixed.

Related run state is committed through the storage adapter as one logical
transition. The filesystem storage adapter atomically publishes
`.ww/tasks/<id>/state.json`, which contains every run, saved plan, execution state,
item/child record, handoff, and ordered ledger event. The document stores an
explicit active-run pointer and validates it against the sole non-completed run;
completed tasks store an explicit null pointer. Start/run creation, dynamic item
expansion, terminal finalization, handoff, and parent/child reconciliation use
this boundary. Writers hold the task lock for the read/modify/commit span and
pass the aggregate revision as a compare-and-swap guard; stale writers are
rejected without changing the authoritative record.

The disk codec is intentionally separate from the expanded domain serializers
and public output. It omits only versioned, record-specific defaults and moves
repeated immutable extension identity/settings snapshots into a root
content-addressed table. Action data stays inside its typed payload in the
expanded plan; the compact document references extension snapshots from that
payload. Decoding restores dense records before existing model
and plan-digest validation, so compaction cannot change execution semantics.
Stored data may come from another ww version, so decoders require the fields
they read and leave any other field alone; they reject contradictions, not
fields they do not know. For the same reason the stored plan digest is
re-derived from this version's reading of the plan when a run is decoded: how a
plan serializes, including which defaults are filled in, depends on the
version, so a digest another version computed cannot be reproduced. Run
directories exist only for artifacts and command output; their names are not an
execution index.

Reads recognize the new document by its `ww.task-state` format discriminator.
The document carries one schema version. A document at another version is
upgraded through the migration table in the codec when an upgrade exists, and
rejected otherwise; the table is empty until a format change ships.
Metadata publication intents are prepared first, state publication is the
execution commit point, and their task/project projections follow that commit.
Scoped cleanup of obsolete regular files follows. Cleanup failures are
diagnostic because the new document has precedence. This is an upgrade boundary:
concurrent writers from pre-consolidation ww versions are not supported.

The separate CLI audit log uses one invocation ID for each `started`/terminal
record pair. User-supplied completion values, recovery output, and failure text
are redacted from the reconstructed command line. Size-based rotation retains a
bounded set of complete JSONL files so operational history cannot grow without
limit; workflow recovery never depends on those files.

The public persistence boundary reflects that source of truth. It is split into
typed run, artifact, task-metadata, and project-metadata ports. The first three
are composed by `TaskStorageAdapter`; legacy `TaskState` methods and
independent plan/state write methods are not part of the contract. Run queries,
item and child reads, handoffs, and run summaries derive from the atomic
aggregate, so a third-party storage adapter cannot satisfy the interface while omitting
data the executor needs.
Missing-record behavior, compare-and-swap errors, locking scope, artifact
reference stability, and whole-task deletion are specified on the ports and
verified by one behavioral suite against the filesystem and memory storage adapters.
Task-scoped metadata lives in the typed metadata port rather than being coupled
to MCP connections or read directly from a filesystem path by the service.
Filesystem persistence projects dotted leaf paths into a nested `metadata`
object in `metadata.json`; other storage adapters expose the same logical values without
emulating that file. The separate project-metadata port persists its filesystem
representation in `.ww/metadata.json`; keeping it outside the task storage adapter
ensures task reset cannot remove shared state. Project metadata uses its own
read/merge/write lock because unrelated task locks do not serialize concurrent
updates. Metadata leaves are strings, and leaf/object collisions are rejected
so interpolation has one unambiguous value for every path.

The supported model layers are normalized authored definitions
(`workflow_config.py`), compiled plans (`plan.py`), and persisted execution
records (the `execution_models` package). During active development, persisted
state uses a single current format. Older task layouts and record schemas are
rejected unless the task-document codec has a migration for them.

## Release guarantees and intentional limits

### Version and compatibility policy

The package version identifies a release, rather than promising that every
internal representation is a public API. Before 1.0, a minor-version increase
may include an intentionally documented incompatible change; patch releases do
not intentionally change documented command-line, persisted-record, or public
extension API contracts. Release notes must call out an incompatible change and
the affected boundary.

Persisted plans and execution records carry their current format versions. The
reader rejects every other version rather than guessing or migrating it.
Plan schema 9 stores each item's operation with an explicit type: registered
actions keep their identifier and type-specific plain payload, and resumed work
uses those saved payloads without resolving workflow configuration again. An unavailable action stays inspectable as saved data; instruction or
execution requests report the missing implementation.
Filesystem layouts, `.ww/` contents, and task records are implementation
details and must not be edited or consumed as a stable external API.

The documented extension API is the compatibility boundary for third-party
extensions. Extensions should declare and test the ww release range they use.
An extension API change is announced in release notes; removed public extension
API is deprecated for at least one minor release when practical. Internal
modules and bundled extensions are not a compatibility promise for external
code.

Recovery and persistence integrity are release criteria, not aspirational
properties. The release-gate suite exercises interrupted automatic handlers,
partial command recovery, publication failures, repeated completion requests,
child-start reconciliation, dynamic plan expansion, extension-store updates,
lock cleanup interleavings, and an extension installed as an independent Python
distribution. Every storage adapter must also pass the same behavioral
contract, including malformed identity rejection and compare-and-swap behavior.
CI runs these focused checks before the complete test suite.

The guarantees for the current local execution model are:

- A run executes from its persisted plan snapshot. That snapshot is immutable
  within a revision; per-item materialization publishes a new numbered revision
  and matching execution ledger as one aggregate transition.
- Plan/state identity includes task, workflow, agent, configuration and
  plan digests, plan revision, ordered item IDs and positions, cursor bounds,
  active-item identity, and command-record correspondence. A storage adapter must
  reject an aggregate that breaks those relationships. The plan digest is
  compared within one ww version: decoding re-derives it, and the runtime
  compares it to catch a plan revised by another process.
- Writes to the same extension-store file are serialized, and replacement
  writes are atomic for unlocked readers. A dependent read-modify-write is
  protected only when the extension uses `update_text`; separate reads and
  writes do not acquire a transaction spanning both calls.
- Interruption after automatic work starts is represented as an unknown outcome
  and is never replayed implicitly, unless the handler itself declared
  `idempotent: true`. A segment that already recorded its exit is a known
  failure, not an unknown outcome. Completed command segments are retained,
  and retry, checker-based recovery, or operator attestation is explicit.
- Atomic replacement writes fsync the temporary file before the rename and the
  containing directory after it, so a committed state survives power loss, not
  only process death.

These guarantees do not provide exactly-once external side effects. An
extension without a checker may require an operator decision, and an extension
that writes outside its provided store is responsible for its own concurrency.
Cross-host locking, distributed scheduling, and automatic reconciliation with
arbitrary remote systems remain intended future capabilities rather than
current behavior.

## Runtimes

A runtime says where manager and worker responsibilities live, not what the work
is. The plan and command protocol are identical in both runtimes, and the
executor never branches on runtime.

`single` is one session switching between both roles. Its defining constraint is
that an agent cannot change its own execution settings, so a `single` run lives
with the session's model and reasoning and does not delegate hooks.

`auto` keeps manager commands and recovery in the main session and gives
each assignment to a worker. The worker submits its own item and hook results
until explicit handoff, then returns a concise outcome and artifact references.
A step may set `subagents: false` to keep its own action in the current session.
For that action, the compiler clears profile and execution-selection hints, so
orchestration cannot accidentally treat inherited or step-level agent, model,
or reasoning values as a delegation request. The setting is persisted with the
compiled item to keep resumed instructions equally local.
A worker may delegate one bounded hook when its runtime permits, but remains
responsible for completing that item; nested workers cannot take over manager
commands or submit the same item independently. Worker selection still belongs
to the manager. The compiled plan preserves the fully inherited requested agent,
model, and reasoning on every item, while assignment and item records preserve
caller-reported selections independently. This separation keeps authored intent
stable when the manager chooses `auto`, a close substitute, or a bounded
delegate for one hook.

`../src/ww/runtimes.py` holds the instruction text and is the single source for the
`runtimes` catalog. Instructions are guidance to the agent, never execution:
nothing in the executor branches on the runtime. The selected runtime and the
manager's start-time model/reasoning are persisted in execution state so a
resumed instruction preserves that guidance. Manager dispatch records the
assignment's intended settings, and each activated agent item records the
reported model/reasoning as execution provenance. ww does not launch a model or
change a live session's settings.

The presentation contract distinguishes durable state from command context.
JSON retains control, role, request, and selection fields for integrations.
Markdown turns the same state into one role and one next action. The public
`status` command projects only the current task identity, workflow step and
state, plus runtime and execution selection, so scripts and agents can inspect
progress without loading role-specific work instructions. `instruction` owns
the full normalized instruction rendering for work resumption and delegation.
`start` and explicit `instruction` service paths mark their instructions for a short
manager introduction; internally generated responses do not, so worker loops
do not repeatedly pay that token cost. Caller role disambiguates an
`auto` handoff from a manager inspecting the same pending state.

## Extensions

Handlers, modes, and commands can come from outside the project. An extension is
`vendor/name`, and `../src/ww/extensions/api.py` is the whole contract a third party
codes against.

Two decisions shape everything else.

**References are always qualified.** An extension is addressed as
`ext/<vendor>/<name>/<section>:<item>` and its items are never merged into the
project's bare namespace. Two extensions may both define `git-commit` and
neither shadows the other or a project handler, so installing one cannot change
the meaning of a name already in use. That property is what makes automatic
discovery safe, and it is why no `extensions:` allow-list is needed: nothing
discovered has any effect until it is named in full.

**Extensions provide no hooks.** A hook decides when work runs against a
particular workflow and step. That is knowledge the project's configuration
holds and an extension cannot; an extension supplies the work, never its
placement.

**Discovery is metadata-only; execution is trusted.** The registry learns
identifiers and source identities from directory layout, bundled resources, and
entry-point names without importing extension modules. It loads code only when
a workflow references that extension or an operator explicitly asks for its
catalog or command. This keeps saved-state commands and unrelated workflows
usable when another extension is broken. Loaded extensions still execute as
trusted in-process Python: qualified names prevent namespace collisions, not
import-time side effects or access to the host process.

An extension handler compiles to a sixth plan kind, `extension` — ww-owned and
automatic, like `cli` — but it runs in ww's own process rather than as a shell
command. That is what lets it remember: `ww/git`'s `git-commit` records each
commit it makes to `../.ww/ext/ww/git/commits.jsonl`, which `./ww extension ww/git
commits` reads back. Each extension's store is namespaced and reached only
through the store object. Writes to one store file are serialized, replacement
writes are atomic for unlocked readers, and `update_text` holds the same file
lock across a complete read–modify–write operation. Extensions must use that
operation when new state depends on old state; separate reads and writes do not
form a transaction, and atomic replacement alone cannot prevent lost updates.

Unlike a shell handler, an extension handler has no per-command ledger: it is
one unit, and an explicit retry re-runs it whole. The extension API can expose
a tri-state checker for interrupted operations; checker errors remain unknown
rather than becoming ordinary handler failures. It can also expose an input
validator for its `provide` values. Core runs it through
`AutomaticAction.validate_inputs` from `complete`, before the step result or
the values are saved, with a context that offers handler lookup only — no
store, workspace, or effects — so a refused value is a refused completion,
not a recorded failure. The same boundary makes retry honest about values: a
failed automatic item that declares `provide` drops those values from the
workflow values when it is returned to pending, keeping them on its record,
so `drain` raises the ordinary input request again and the page can show what
the handler was given last time. The compiled item stores the
reference and declared output names rather than the callable, so a persisted
snapshot stays readable without loading anyone's code, and an unknown extension
or item fails at compile time — before a task exists.

The item also freezes the public API version, extension version, provider
source, source fingerprint, and that extension's settings. Dispatch uses the
frozen settings and rejects a different version, API version, or provider
source. The fingerprint is recorded for audit only: like ww's own code, an
extension may be fixed in place while a task is in flight, so a change in
behaviour is signalled by a version bump rather than by the bytes of the file.
Project-extension fingerprints cover `extension.py`; installed-provider
fingerprints cover distribution name, version, and entry-point metadata. The
current plan schema requires identity metadata; older plan snapshots are
rejected.

Extension settings live in `../agentic-workflows.json`. Initialization merges
missing defaults without replacing existing extension choices. The split is
deliberate: `../workflows.yaml` says what a
workflow does, and the project config says how the tools around it behave. ww
validates the file's shape and hands each extension its own section untouched —
it cannot know a third party's schema, so an extension validates its own
settings and reports its own errors. The cost is that a typo surfaces when a
handler first runs, which is why `ww/git` carries a `settings` command that
prints what resolved. A section naming no installed extension is rejected: a
block that silently applies to nothing is worse than an error, because the file
looks configured and the behaviour never changes.

Discovery registers ww's bundled extensions first, then
`<root>/ext/<vendor>/<name>/extension.py`, then `ww.extensions` entry points.
The `ww/git` source remains in the repository's `../ext` directory for local
development, but Hatch includes it as package data and the registry loads it as
a bundle; editable installs fall back to that source path. This means a target
project never has to copy `ww/git` merely because ww was installed elsewhere.
Duplicate identifiers remain an error rather than a precedence rule, so a
project cannot silently replace a bundled extension. Packaged entry points use
`vendor.name` as their entry-point name so the registry can identify and
deduplicate them without importing their modules. On load, the public API
version, contribution types, normalized names, unique handler/mode/command/variable
names, and callables are validated at the boundary; handler and command return
shapes are validated after invocation.

Variable overrides are intentionally narrower than handler outputs. A referenced
extension may override only a variable already defined by core; it cannot add a
new global name, and two extensions cannot claim the same variable. Overrides
are evaluated from live task context, while unreferenced extensions remain
unloaded. This preserves explicit activation and lets `ww/git` recover the task
workspace from its recorded worktree when persisted working-directory state is
not sufficient.

## Concurrency

Two `ww` invocations in one project are unrelated processes competing for the
same files, and nothing in the CLI's shape prevents an agent, a human, and a
hook from overlapping. The failure that matters is silent: two processes read
one `state.json`, each decide the next item, and one overwrites the other, with
no error anywhere. Atomic replacement does not help — it only guarantees that a
reader never sees half a write.

The fix is one exclusive lock per task, held by `WorkflowService` across a whole
`start`, `next`, `complete`, or `reset`. The span is the unit that needs
protecting, so that is the only place the scope is taken: the persistence
storage adapter's methods assume the caller holds it, and `lock_task` on the port is the
documented boundary, defaulting to a no-op for storage adapters with no shared storage.
Two further scopes cover what no task owns — `ww init`, and the
`../.ww/executions.jsonl` log every command appends to across tasks.

Reads are deliberately unlocked. Replacement is atomic, so a reader always sees
a whole aggregate document; `RunCoordinator.load` selects the run, state, and plan
snapshot from that single decoded revision. Locking reads instead would make
`instruction` and compact `status` reads queue behind a command that may be running a CLI handler—the command you
reach for when something looks stuck.

Locks are advisory POSIX locks on sidecar files under `../.ww/locks`, never on the
guarded file itself, because atomic replacement swaps the target's inode and
`reset` deletes whole task directories. The kernel drops them when a process
dies. Handoff order between waiters is unspecified — no operating system
promises FIFO — and `WW_LOCK_TIMEOUT` bounds every wait so real contention fails
loudly rather than hanging.

Sidecar paths intentionally outlive the lock that used them, so their presence
does not mean a task is stuck. `ww cleanup` takes an exclusive maintenance gate
while normal lock users hold it shared; it can therefore remove inactive
sidecars only after every holder and waiter has left, without splitting a race
between old and newly created lock inodes. The command log records an operation
as `started` before work begins and adds `ok` or `error` when it returns. This
leaves useful evidence when an outside runtime terminates a command mid-flight.

## Deferred nesting

Definition models support recursive child steps. The compiler retains every
parent lifecycle boundary while emitting leaf work as a flat sequence with a
hierarchical step path and immediate-parent reference. The executor persists the
matching state tree: a parent begins with its first child and completes after
its last child, without forcing plan rendering or agent instructions to become
nested.

Hook filters share that hierarchy. Bare names retain a convenient broad match,
but an exact logical path takes precedence when a wrapper and nested leaf share
a name. This lets completion hooks target the wrapper boundary without firing
inside its child sequence. Slash-separated logical paths select one nested
branch precisely. Logical paths exclude the runtime item identifier inserted for per-item
stages, keeping hook configuration stable across dynamically collected items.

General workflows nested inside workflows, returning workflow calls, and chained
handoffs remain deferred.

## Task runs

A task is a durable container rather than a single workflow execution. Every
sequential workflow run persists its plan snapshot, execution state, and ledger
history in the task-root `state.json`. Numbered run directories contain only
artifacts or command output when those exist. Separate task-root metadata keeps
the task identity and cross-run values independent from execution publication.
This separates immutable run history from the task identity needed by future
external trackers such as Jira.

The task root also retains the generic metadata object shared by all of those
runs. Agent completions can update only paths declared by the active plan item,
while prompts, automatic commands, extension contexts, and workflow transitions
can consume them through the `metadata` interpolation namespace. The
`metadata <task-id>` command exposes the complete object as JSON for inspection
without making the execution aggregate the source of truth for it.

Project metadata provides the same validated dotted-path model across task
containers through the explicit `project_metadata` namespace. It is read live
when actions are rendered, supporting project-wide discoveries that evolve over
time; workflows needing a stable per-task value must capture that value into
task metadata. `metadata --project` exposes the shared object without coupling
its lifetime to any task.

The compiler appends ww's internal `update-workflow-summary` action as the
final `before_complete_workflow` handler for every non-handoff workflow. It
supplies the concise summary value, and the executor records it only after that
handler completes, keeping the task-wide ledger aligned with durable workflow
completion without requiring workflow configuration.

## External task identity bootstrap

Most tracker-backed workflows do not know their durable ID until an agent has
created or fetched the external issue. The first declared workflow step may
provide exactly `task_id` and therefore has a narrow bootstrap role when `start` is called
without an ID. ww records that short-lived request under `../.ww/bootstrap`, runs
only that agent-owned action, and binds the returned normalized ID before it
creates any task state, branch, worktree, run ledger, or ordinary lifecycle
hooks. This avoids a misleading generated `TASK-*` directory becoming the
identity of a Jira-backed task.

Bootstrap binding is resumable: the request is marked `binding` before the
task aggregate is published and `completed` afterwards. A retry reuses the
persisted external ID, either publishing the missing aggregate or completing
the request marker for an aggregate that was already published, without
starting a duplicate run.

Once bound, ww starts the regular workflow under the external ID and preserves
the bootstrap artifact alongside the run's step artifacts. The identity action
itself is omitted from the ordinary plan because it has already happened; normal
`before_start_workflow` hooks and the implicit `init` step then run against the
real task. An explicit task ID is
authoritative, so `task_id` cannot be supplied by a normal completion. Keeping
this as a special first-step contract — no hooks, nesting, or additional
provided values — makes the boundary visible and lets tracker integrations stay
ordinary MCP-backed agent work instead of becoming a Jira-specific subsystem.
Both omission of an already-completed bootstrap action and removal of an
authoritative `task_id` input are compilation options. The service does not
rewrite a compiled plan, and `plan` can therefore request the same
interpretation that `start` persists.

## Task worktrees

An extension may select a working directory for a run. The git extension does
so when it creates a task worktree. The executor persists that directory with
the run state, exposes it in agent instructions, and uses it for later shell
and extension handlers. This keeps branch creation, agent work, and automatic
commits in the same checkout instead of silently falling back to the root
worktree.

The same selection is exposed to workflow interpolation as
`{{__task_workspace_dir}}`. Core resolves it to the canonical project root (or the
persisted working directory); referenced extensions then receive a constrained
override opportunity. The Git override prefers the primary checkout when it is
on the exact task branch and otherwise uses the selected or recorded worktree.

An agent instruction calls the selected checkout the task workspace and makes
it explicit with a `cd` command. The neutral name covers both a linked worktree
and the primary checkout when it already uses the configured task branch.
When an operator invokes ww from that linked worktree without `--root`, CLI
root discovery follows Git's common directory back to the primary checkout, so
the task ledger, `.ww` runtime, and configuration remain shared rather than
being recreated inside the worktree. An explicit `--root` remains authoritative.

The git extension separates `start-task-branch` from `create-worktree`. The
first establishes the task branch without occupying it when worktrees are
enabled; the second may be placed independently in workflow hooks and is a
no-op when worktrees are disabled. If the primary checkout already uses the
task branch when either handler runs, it is selected as the task workspace.
This lets an operator deliberately use the primary session for task work
without ww creating or directing work back to another checkout.

Branch-name formats are named strategies in the Git extension configuration.
Normally the workflow name selects a strategy before the extension falls back
to `default`; `start --branch-strategy` records an explicit selector in the
run's durable value environment. Keeping the selector with execution state,
rather than changing project configuration for one invocation, ensures delayed
hooks, retries, and bootstrap ID binding resolve the same branch name. An
explicit name must exist, because silently falling back would make the CLI
override misleading.

Base-branch selection belongs to the Git extension's project settings rather
than the workflow plan. A global literal or command-backed base can be replaced
for a named workflow; structured `argv` execution keeps arguments separate from
shell syntax. The selected branch is recorded when the task branch is created,
so later worktree creation, retries, and return-to-base behavior use the same
resolved value even if a command would subsequently produce something else.

The git extension treats that selected checkout as a safety boundary. Before
its commit handler stages work, it confirms the workspace is the Git worktree
root and validates every changed path; it then validates the staged index again
before committing. This makes a mistaken workspace or an escaping path a hard
failure instead of silently committing from a different checkout.

After staging, the handler also requires a non-empty index before it invokes
`git commit`. This keeps hook failures meaningful in repositories that use
pre-commit tooling such as Husky and lint-staged: an ignored or otherwise empty
change set is reported by ww directly instead of being misrepresented as a hook
integration failure.

## Parent and child tasks

One workflow can decompose a task into independently executable child tasks.
The parent owns the decomposition through a `children: ~` step, where the agent
records each child with `ww add-child`. A later `workflow_per_child: <workflow>`
step is a coordinator boundary rather than agent work: `ww child start <parent>
<child>` starts one selected child workflow at a time.

Children are normal task containers stored directly beneath their parent, for
example `.ww/tasks/TASK-123/TASK-123.1/`. They retain their own run history,
artifacts, items, hooks, and workflow summary, while the parent's run-local
`children.json` records the selected child workflow, status, and short result.
This keeps the parent status meaningful without letting either workflow mutate
the other's plan or artifacts.

The scope is deliberately one level: a child workflow cannot itself declare
`children` or `workflow_per_child`. When the last child completes, ww completes
the parent coordinator and resumes the parent's ordinary completion lifecycle,
including its `after_complete` hooks and built-in workflow summary. A failed
child instead marks that coordinator failed so the parent does not appear done.

Child publication and parent notification are separate task commits, so terminal
reconciliation is repeatable. A child `next` or `recover` retries notification,
and a waiting parent's ordinary `next` refreshes every child outcome from the
authoritative child aggregates before deciding whether to keep waiting. Repeating
that refresh after the parent has advanced or completed is safe.

A child handoff preserves its parent and start-operation binding while giving
the successor its own execution-instance identity. Reconciliation follows the
latest run with that binding, even when its workflow changes, so completing a
selection run cannot complete the parent while the successor still has work.

When the git extension is configured to create task branches, a child starts
from its parent task branch rather than the global base branch. Git cannot have
a branch and a slash-nested branch beneath it simultaneously, so the child
branch appends its local child ID to the parent branch with a dash.

## Projects and task working directories

The project root and a task's working directory are different things. The root
owns configuration, `.ww` state, artifacts, and locks; the working directory is
where a task's commands, hooks, and extension handlers run. The root is
resolved on every invocation, so it is always right for the filesystem the
process runs in; everything ww persists that names a location, the working
directory and a step's profile file, is stored relative to the root and
resolved against it when used or printed (`ww.workspace`). A checkout mounted
at another path, such as inside a container, therefore reads the same state
and prints paths valid there. Until now the two
coincided unless a Git worktree moved a task. The optional `projects` list in
`agentic-workflows.json` makes the distinction explicit; it belongs to the
machine-specific settings file because checkouts are laid out differently on
each machine while the workflows are shared: `start --project` or
`add-child --project` resolves a configured directory and persists it as the
run's working directory, records the project name in the run's workflow values,
and a handoff successor inherits both. Core knows nothing about repositories;
it only resolves a name to a directory that must exist. The `ww/git` extension
derives the repository from the task's working directory, resolving a worktree
back to its primary checkout through the shared git directory, so one
extension serves single-repository projects and multi-repository workspaces
without configuration.

## Documents

A document is the durable complement of metadata: a free-format file declared
once at the root, resolved to a task or project location, and edited in place
by the agent that a step's `update_document` names. Core deliberately owns as
little of it as possible. The compiler freezes the declarations into the plan
so a run resolves paths without the configuration, the compiler admits
`documents.<name>` as an interpolation name, and completion checks that each
promised file exists before journaling run, step, time, and content hash. The
journal, not the file, is what state knows; the file's content and format stay
the workflow's. The document store and the interaction log are the two task
records kept beside the storage adapter, on the filesystem under the task
directory; `reset` asks each to forget the task before the adapter removes
what it owns, so a task started later under the same ID inherits nothing.

## Children that bind their own identity

A child added without an ID under a child workflow whose first step provides
`task_id` is recorded under a temporary request ID, the same shape the
top-level bootstrap flow uses. `child start` then opens an identity request
bound to the parent instead of a run. Completing the request validates the
supplied ID as one segment beneath the parent, starts the child run with the
identity step already done and the parent binding recorded, and finally renames
the parent's child record. The binding runs inside the request's lock and is
idempotent, so a retried request after a crash finds the bound child and only
repairs the parent record. The compiler flags the children collection with
`child_identity` when the child workflow binds identity, so the collection
instruction can tell agents not to pass `--id` without loading the child plan
at render time.

## Workflow items

Some work is only enumerable after an agent has inspected an external source,
such as the comments on a pull request. Items are therefore durable, run-local
records rather than configuration-time loop values. One `items` step owns the
whole flow: its own action is the collection, annotated `collect` and carrying
any splitting guidance, and its completion expands the saved per-item templates
into concrete plan items. A bare `items` step compiles one built-in
`handle-item` template with the combined `handle_item` operation. This
preserves the executor's ordinary retry, artifact, and status rules while making
each item's progress independently visible.

Item-flow worker settings are folded into each configured stage while parsing,
so the compiler sees ordinary steps; where stages still differ, the assignment
bound splits at run time rather than the compiler rejecting the flow. Only the
collection item carries `collect`: the `items`
step's hooks wrap the entire flow, and a completion hook tagged `collect` would
re-trigger expansion after the last item. Each workflow allows one `items` step,
because collected items are run-scoped and every template expands at the first
template position.

Per-item templates are full steps, not merely work handlers. Their applicable
global, workflow, and step hooks are compiled into the template segment and
expanded beside its main action for each item. That keeps lifecycle guarantees
identical whether work is known when the workflow starts or discovered later.
Completing the collector ends its assignment before the first concrete item;
the manager dispatches each materialized lifecycle independently.

Each record retains both the source text and its analysis, proposed and actual
solution, resolution/reporting flags, and an optional canonical-item reference.
Related source comments can share work without disappearing from reporting.
The item commands are the only mutation boundary, which keeps external agents
from editing task files directly. The executor refuses to leave an item phase
until every collected item is both resolved and reported.

A flow declared `shared` adds one task-level store beside the per-run copies,
through the storage adapter like metadata. The run's copy stays the working
set and the source of every expansion; the store is written from it after
each item command, so it holds the latest outcome of every item, and a new
run is seeded from it at start with outcomes cleared. Reconciling lives in
the collection step's page and two collection-time commands rather than in
the engine: the compiler marks the collection item, the seeding happens at
run start, and nothing about expansion or per-item execution changes.

Custom fields keep the item a closed record: a string mapping on the work
item, declared per step with `update_item` and gated at completion exactly
as `update_metadata` is, so a step cannot finish having promised a field it
did not set. `identity` and `unique` are flow-level rules the compiler
carries on the collection item and the item commands enforce, against the
run's items and the shared store, because a duplicate an agent could add by
reasoning wrong must be a refusal, not an instruction.

## Interactive steps and the operator page

ww cannot hear a conversation between the agent and the operator, so an
interactive step owns only the record and the gate: `interact` appends both
sides to one append-only file per task, the step record counts entries and
remembers whether the operator ended or paused the conversation, and
completion is refused until it was ended. How the operator is asked is the
agent's business; the choice mechanisms table in `agents.py` is core knowledge
of what each integration offers, not an extension point.

The operator page is an extra on top of that, not part of it. The core knows
it by one flag, `ui` on a per-item stage, which the step's page turns into
one command; the `operator_ui` package owns the rest and drives the task only
through the public service calls an agent uses: `interact` to record the pick
and the comment and end the stage, `update_item` to mark the built-in stage's
item, `complete`, and `next`. Nothing in it writes task state, and the
engine, the records, the work items, and the instruction builder know nothing
about answers or pages.

An answer is operator input the engine has not acted on, so it stays out of
the task's state: it is one atomic replacement of the package's own sheet
file under the task lock, before the browser is acknowledged, so a lost wait
loses nothing and the state changes only through the engine's own commands.
The sheet carries the task's creation time and discards itself for a task
that was reset. The page is served by `interact --await`, a command the
agent runs and blocks on, for exactly as long as it waits; there is no
daemon and no state in the server. When the wait ends, the package consumes
the sheet in plan order, and a stage is committed before its answer leaves
the sheet, so a cut wait leaves a stale entry that the next wait drops. The
pairing of an answer with the stage it completes is unambiguous because an
item flow may declare one `ui` stage. The port is derived from the task ID
so an open tab survives between waits, and the wait is bounded so an agent's
shell timeout never kills it mid-way. A pause is kept on the execution
state, not on a stage, because it outlives the stage that was current when
the operator left, and only the operator's own words lift it.
