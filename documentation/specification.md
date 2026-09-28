# `../workflows.yaml` specification

`../workflows.yaml` defines what `ww` workflows do. The file is strict: unknown
keys, invalid types, and invalid references are errors.

## Common types

- **Name:** a non-empty string matching `[A-Za-z_][A-Za-z0-9_.-]*`.
- **Extension reference:** a qualified name such as
  `ext/ww/git/handlers:git-commit` where explicitly supported.
- **Description:** a string. It is optional unless stated otherwise.
- **Template:** `{{variable}}`. Available values include earlier `provide`
  results, extension outputs, `{{__task_workspace_dir}}`, `{{__task_id}}`, `{{__workflows}}`,
  `{{__project}}`, `{{__project_dir}}`, `{{__projects}}`, `{{metadata.<path>}}`,
  `{{project_metadata.<path>}}`, and on a per-item stage `{{item.id}}`,
  `{{item.text}}`, and `{{field.<name>}}` for the stage's own item.

Names must be unique within their catalog or sibling step list.

## Root

| Key | Type | Required | Meaning |
| --- | --- | --- | --- |
| `workflows` | list of workflows | yes | At least one workflow is required. |
| `task_format` | string | no | Generated task ID format. Supports `{timestamp}`, `{digit}`, and `{uuid}`. The value `explicit` forbids generated IDs: every task needs an explicit ID unless its workflow binds one. |
| `modes` | list of modes | no | Reusable agent guidance. |
| `profiles` | mapping | no | Named agent profiles. |
| `documents` | list of documents | no | Durable, free-format files that workflows read and update across runs. |
| `handlers` | list of handlers | no | Reusable actions referenced by hooks. |
| `hooks` | hooks mapping | no | Hooks applying across workflows. |

The legacy root key `tasks` is rejected.

Projects, the directories a task may work in, are configured in
`agentic-workflows.json` rather than here because their locations differ per
machine; see the features guide.

## Modes and profiles

A mode has these keys:

| Key | Type | Required |
| --- | --- | --- |
| `name` | name | yes |
| `description` | string or list of non-empty strings | no |

Modes accept the named-entry shorthand too: `- economy: Use fewer tokens.` is
equivalent to `{name: economy, description: Use fewer tokens.}`. A null value
omits the description.

Profiles use a mapping rather than a list:

```yaml
profiles:
  developer: Prefer small, well-tested changes.
  reviewer: ~
```

Each profile key is a name. Its value is a non-empty string or `null`.

## Workflows

Each item in `workflows` accepts:

| Key | Type | Required | Meaning |
| --- | --- | --- | --- |
| `name` | name | yes | Workflow identifier. |
| `steps` | list of steps | yes, unless `inherit` is set | Ordered declared steps; may be empty. The implicit `init` step is not listed. |
| `description` | string | no | Human-readable purpose. |
| `hooks` | hooks mapping | no | Hooks for this workflow. |
| `modes` | list of names or extension references | no | Default modes; every local name must exist. |
| `agent` | non-empty string other than `auto` | no | Preferred executor; currently advisory. |
| `model` | non-empty string | no | Default model guidance; `auto` stops inheritance. |
| `reasoning` | non-empty string | no | Default reasoning guidance. |
| `profile` | profile value | no | Default agent profile. |
| `handoff` | boolean | no | If `true`, the workflow must end with its single workflow transition; a task hands off at most once, and a transition never returns. |
| `runtime` | `single` or `auto` | no | The runtime `start` uses for this workflow when `--runtime` is omitted; it outranks the project default in `agentic-workflows.json`, and the flag outranks it. |
| `restartable` | boolean | no | A new `start` of this workflow while its previous run is unfinished abandons that run and opens a new one; the abandoned run stays in the task's history. Without it, a task with an unfinished run refuses another start. An unfinished run of a different workflow is never abandoned this way. Defaults to `false`. |
| `inherit` | workflow name | no | Copy that workflow completely: steps, workflow hooks, and every setting. The workflow's own keys other than `steps` and `hooks`, which it may not declare, replace the copied values. A global hook filtered to the inherited workflow also runs for this one. Chains are allowed; a cycle or unknown name is an error. |
| `recommended_next_workflow` | workflow name or null | no | Offered to the operator when a run completes: the page asks through the agent's choice menu and shows the `start` command for the same task, to run only on confirmation. Inherited like any setting; `null` clears an inherited one. Invalid together with `handoff`. |

Execution settings inherit from workflow to enclosing steps to the current
step. Hooks then apply the referenced root handler and the invocation override.
Omission inherits; explicit `model: auto` or `reasoning: auto` stops inheritance.
Model and reasoning are coupled: when a child changes the effective model and
omits reasoning, reasoning resets to `auto`. Repeating the same model preserves
the inherited reasoning. `agent` falls back to the agent passed to `plan` or
`start` and may not be `auto`.

`../agentic-workflows.json` sets ww-wide project behavior separately from workflow
syntax. `loop_max_times` is a positive integer and defaults to `3`. The file may
also override the internal requests of the implicit init action, `cheapest` /
`low`, and of the workflow-summary action, `auto` / `auto`. `workflows` switches
off the workflows ww provides to every project, currently only `catchall`; each
entry is an object whose only key, `enabled`, defaults to `true`. A workflow of
the same name in `workflows.yaml` replaces the provided one instead.
`executable` names the ww binary the project runs, a command on `PATH` or a
path; every command ww prints starts with it, and the `./ww` launcher runs it.
Without it, printed commands use `./ww` and the launcher runs
`ww-agentic-workflows`; `init` writes `ww-agentic-workflows` when it is
missing. Missing fields retain their individual defaults:

```json
{
  "executable": "ww-agentic-workflows",
  "loop_max_times": 3,
  "builtins": {"init": {"model": "fast"}},
  "workflows": {"catchall": {"enabled": false}},
  "extensions": {}
}
```

Workflows cannot be nested. A profile value is either a name or a mapping with
`name` and/or `description`:

```yaml
profile:
  name: reviewer
  description: Focus on correctness and regressions.
```

Workflows accept the [named-entry shorthand](#named-entry-shorthand). For
example, `- task: Run the standard development workflow.` supplies both the
name and description; `steps` and other workflow keys remain siblings.

## Documents

`documents` declares durable files by name. Their format is entirely the
workflow's business; ww knows only the name, a description, the scope, and
which step last updated the file. A task-scoped document lives in the task
directory, `.ww/tasks/<task-id>/documents/<name>.md`, and persists across every
run of that task; `scope: project` puts it in `.ww/documents/<name>.md`, shared
by all tasks:

```yaml
documents:
  - test_cases: The test cases derived from the issue, kept current across runs.
  - conventions: Conventions every task follows.
    scope: project
  - issue_notes: Notes kept with the repository, on the task's branch.
    path: documentation/issues/{task_id}/notes.md
```

`path` places a document outside `.ww`. It is relative to the project root and
must stay inside it; a task-scoped path may use `{task_id}`. When a run has a
working directory, such as a Git worktree, a task-scoped `path` resolves inside
that directory, so a document kept in the repository lands on the task's
branch; a project-scoped `path` always resolves against the project root. The
update journal stays under `.ww` in every case.

A step or handler that maintains a document declares `update_document`, naming
the document and instructing the update. Its worker may create or edit that
file in place, the one exception to the rule against writing under `.ww`, and
the file must exist when the step completes. `{{documents.<name>}}` in any
description resolves to the file's absolute path on the filesystem ww runs in,
whether or not the file exists yet:

```yaml
- derive_tests: Analyze the issue and derive test cases.
  update_document:
    - test_cases: One checklist item per case; keep items that still hold, mark superseded ones.
- report: Build the test report from {{documents.test_cases}}.
```

## Steps

Every workflow has a built-in first step named `init`. It is omitted from
`steps`, and declaring `init` anywhere as a top-level, nested, or per-item step
is an error because the name is reserved. Its fixed agent prompt records the
passed task requirements with corrected grammar and style, without analysis or
planning. It always produces an artifact, uses low reasoning, and does not
inherit the workflow profile.

The implicit step otherwise participates in the standard step lifecycle.
`before_start_workflow` runs once before `init`; it belongs at global or workflow
scope and does not accept a `steps` filter. Use `before_in_progress` for
preparation local to `init` or another step. A top-level declared step may use
`depends_on: init` to
receive the requirements artifact. Handoff targets and child-task workflow runs
each begin with their own `init` step.

A step accepts every [handler key](#handlers), plus:

| Key | Type | Meaning |
| --- | --- | --- |
| `hooks` | hooks mapping | Hooks local to this step. |
| `profile` | profile value | Overrides the profile inherited from the workflow and every enclosing step. Nested steps, loop bodies, and per-item stages inherit it in turn. |
| `subagents` | boolean | When `false`, an `auto` run performs this step without delegation and ignores its profile, agent, model, and reasoning settings. Defaults to `true`. |
| `interactive` | boolean | The step is a conversation with the operator, held by the session that can talk to them; it implies `subagents: false`. Its completion is refused until the conversation was recorded with `interact` and ended. Defaults to `false`. |
| `choices` | list of choices | Options the operator picks from during an interactive step, `- <label>: <description>`; the label is shown as written. The agent offers them through its own question tool, `AskUserQuestion` in Claude Code, `request_user_input` in Codex, `ask_user` in Gemini CLI, `AskQuestion` in Cursor, `ask_question` in Antigravity, `ask_user_question` in Grok CLI, and a numbered list elsewhere or where the tool is unavailable, and the pick must be recorded before the interaction ends. Requires `interactive: true`. |
| `ui` | boolean | The operator answers this stage on the operator page, an answer sheet over every item that `interact --await` serves while the agent waits and applies when the wait ends. Valid on one per-item stage per `items` step and requires `interactive: true`. Defaults to `false`. |
| `update_item` | list of item fields | Custom fields this step sets on its item, `- <name>: <what to put there>`; on the collection step, on every collected item. Completion is refused while any is empty. Set them with `update-item --field NAME=VALUE`, several per call. |
| `steps` | list of steps | Nested ordered steps. |
| `loop` | non-empty list of steps | Repeats ordinary nested steps until an authorized worker stops it. |
| `loop_max_times` | positive integer | Overrides the project-wide maximum for this loop. Valid only beside `loop`. |
| `loop_assignment` | `per_iteration` or `per_step` | How the body steps are split into worker assignments in the `auto` runtime; default `per_iteration`. Valid only beside `loop`. |
| `break` | non-empty string | On an agent-owned loop-body step, grants permission to break its enclosing loop when this condition holds. |
| `continue` | non-empty string | On an agent-owned loop-body step, grants permission to continue from the beginning of its enclosing loop when this condition holds. |
| `artifact` | boolean | Whether agent completion requires an artifact; default `true`. |
| `depends_on` | name | Earlier artifact-producing sibling whose artifact is supplied to this step. |
| `items` | `null`, string, or mapping | Collects work items, then runs per-item stages for each; see [Items](#items). |
| `workflow` | workflow name or `{{variable}}` | Ends a `handoff: true` workflow by starting that workflow as the task's next run. Valid only on the last step, with `description`, `agent`, `model`, and `reasoning` at most; or on a hook, see [Hooks](#hooks). |
| `process_item` | `null` | Marks the step as updating processed item data. |
| `resolve_item` | `null` | Marks the step as resolving an item. |
| `report_item` | `null` | Marks the step as reporting an item. |
| `children` | `null` | Marks a child-task collection step. |
| `workflow_per_child` | workflow name | Runs that workflow for every collected child. |
| `handler` | handler name | Copies a root handler definition into this step; the step keeps its own name and any explicit step fields override the copied values. A step with no content of its own, `- fetch_requirements: ~`, and a root handler of the same name copies that handler implicitly. |

`steps`, `loop`, and `items` are alternatives. A loop wrapper cannot
also declare an action or collection; its `loop` entries are ordinary steps and
may use hooks, profiles, item collection, nested steps, and the other step
features. A step may have at most one of
`process_item`, `resolve_item`, and `report_item`. Marker keys must contain YAML
`null` (`~`).

The manager enters a loop and dispatches its body. With the default
`loop_assignment: per_iteration`, one worker carries consecutive body steps of
one round while they resolve to the same agent, model, reasoning, and profile;
a body step that overrides any of them starts a new assignment, because a
running worker cannot change them. `per_step` hands every body step back to
the manager. Every body step's instruction states which round of the loop it
belongs to, so a later round concentrates on the previous rounds' work rather
than on the whole task. One or more agent-owned steps may declare `break` or
`continue`. After doing such a step, its
worker evaluates that step's break gate. If it passes, the worker runs the
displayed `ww loop <TASK-ID> --break --role worker` command instead of `complete`;
that command records the step result and deterministically exits the enclosing
loop after the step's completion hooks. The break result is also saved as the
loop wrapper's main artifact, outside its iteration directories. A wrapper may
set `artifact: false` to disable only this main artifact; body steps retain
their own artifact settings.

If a worker uses `continue`, ww records the result, runs the step's completion
hooks, then resets the body and dispatches its first step. If no worker breaks
or continues the loop, reaching the end resets the body and the manager
dispatches the first step again until the effective maximum is reached. The
effective value comes from the wrapper's `loop_max_times`, or from
`../agentic-workflows.json` when the wrapper omits it, and is frozen in the saved
workflow plan. At the limit, ww does not expose a continuation command: it
blocks manager control with a warning that must be escalated to the user for
manual resolution, and shows the operator's exit, `next --force --force-reason`,
which leaves the loop and continues with the steps after it.

`loop_max_times` is invalid without `loop`. The obsolete `stop` spelling is
rejected; use `break` for a worker-controlled loop exit.

```yaml
- code-review-in-a-loop: ~
  loop_max_times: 5
  loop:
    - code-review: Review the development or the previous round of fixes.
      break: There are no meaningful code-review findings.
    - fix: Record each code-review finding as an item.
      items: ~
```

A workflow may contain at most one `children` step and one
`workflow_per_child` step. The latter requires the former and must name an
existing workflow. Child workflows cannot create further child tasks.

Steps also accept the [named-entry shorthand](#named-entry-shorthand). For
example, `- develop: Implement and test the change.` is equivalent to a step
with `name: develop` and that text in `description`.

To reuse a root handler as an ordinary step, set `handler` beside the step's
name. This is a definition copy, not an additional action: the compiled plan
contains only `some_step`, which uses the copied handler action. The step's
description and explicit handler fields override the copied values. A root
handler can also define a complete step container (`steps`, `loop`, or
`items`); a referencing step inherits that tree and retains its own name as the
outer step identity.

```yaml
handlers:
  - name: handler_name
    argv: [printf, ready]

workflows:
  - name: task
    steps:
      - some_step: Run the shared check for this workflow.
        handler: handler_name
```

For example, a reusable review loop can be declared once and used by any
workflow:

```yaml
handlers:
  - code-review:
      loop:
        - code-review: Perform the code review.
      continue: There are no meaningful review remarks.
          profile: code-reviewer
        - fix: Fix the review findings.
          profile: developer

workflows:
  - name: task
    steps:
      - code-review: ~
        handler: code-review
```

A name-only extension handler reference can be used directly as a step, for
example `- ext/ww/git/handlers:is-git-clean: ~`. It is resolved and validated
when the workflow plan is compiled, just like an extension handler in a hook.

### Items

A step with `items` owns a whole item lifecycle. Its own action is the
collection stage: the agent performs the step's work and records each resulting
work item with `add-item`. When collection completes, ww expands the per-item
stages once for every collected item. The minimal form automates as much as
possible:

```yaml
- review: Review the pull request.
  items: ~
```

Every item then gets one built-in `handle-item` stage that analyzes, resolves,
and reports it in a single pass.

A string is splitting guidance for the collection stage. It tells the agent how
to split the work, and it does not describe the per-item stages:

```yaml
- review: Review the pull request.
  items: Split based on the comments retrieved from the Bitbucket pull request.
```

The mapping form accepts these keys, all optional:

| Key | Type | Meaning |
| --- | --- | --- |
| `description` | non-empty string | Splitting guidance for the collection stage; the same as the string form. |
| `process_item`, `resolve_item`, `report_item` | non-empty string | Guidance for the analyze, resolve, or report phase of the built-in `handle-item` stage. Invalid together with `steps`. |
| `update_metadata` | list of metadata values | Values the built-in `handle-item` stage saves on each item's completion. Invalid together with `steps`. |
| `update_document` | list of document updates | Documents the built-in `handle-item` stage updates on each item's completion. Invalid together with `steps`. |
| `interactive` | boolean | Makes the built-in `handle-item` stage a conversation with the operator, for example a manual test the operator performs and reports. Invalid together with `steps`. |
| `choices` | list of choices | Options the operator picks from in the built-in `handle-item` stage. Invalid together with `steps`. |
| `ui` | boolean | The operator answers the built-in `handle-item` stage on the operator page, and `interact --await` completes it from the answer. Requires `interactive: true`; invalid together with `steps`. |
| `steps` | list of steps | The per-item stages. Omitted, one built-in `handle-item` stage runs per item. `[]` collects items without processing them. |
| `item_assignment` | `all_items`, `per_item`, or `per_step` | How per-item stages are split into worker assignments in the `auto` runtime; default `all_items`. |
| `shared` | boolean | The items outlive the run: every run of the task starts from the task's stored items with their outcomes cleared, and the collection step reconciles that list against the source instead of splitting again. Defaults to `false`. |
| `identity` | field name | The custom field every new item must carry; `add-item` refuses one without it. Implied in `unique`. |
| `unique` | list of field names | One pool of values across the listed fields: a value may appear once over all items, in the run and in the task's stored items. `add-item` and `update-item` refuse a duplicate and name the item that holds it. |
| `agent` | non-empty string other than `auto` | Agent for the per-item stages. |
| `model` | non-empty string | Model for the per-item stages. |
| `reasoning` | non-empty string | Reasoning for the per-item stages. |
| `profile` | profile value | Profile for the per-item stages. |
| `subagents` | boolean | `false` performs the per-item stages without delegation. |

```yaml
- review: Review the pull request.
  model: opus
  items:
    description: Split by pull request comment.
    item_assignment: per_item
    model: sonnet
    reasoning: low
    steps:
      - analyze: Analyze this comment.
        process_item: ~
      - fix: Resolve this comment.
        resolve_item: ~
      - reply: Report the outcome.
        report_item: ~
```

Worker settings cascade from the `items` step, to `items`, to each stage, and
the most specific value wins. The step's own `agent`, `model`, `reasoning`,
`profile`, and `subagents` apply to collection and are inherited by the stages;
the same keys under `items` apply only to the stages. The cascade reaches each
configured stage directly; stages nested deeper inherit like ordinary nested
steps.

`item_assignment` changes only where worker assignments end in the `auto`
runtime:

- `all_items`, the default, gives one worker every stage of every item,
  followed by the `items` step's completion hooks.
- `per_item` gives one worker every stage of one item, including stage hooks;
  the next item starts a new assignment.
- `per_step` dispatches every stage as its own assignment.

It has no effect in the `single` runtime, where one session already performs
every assignment. A running worker cannot change its agent, model, reasoning,
or profile, so a stage that resolves to different settings than the worker's,
or that sets `subagents: false`, starts a new assignment; the span resumes
with the next stage that matches. Set shared settings on `items` to keep a
whole span with one worker.

The step's `before_in_progress` hooks run before collection, and its completion
hooks run after the last item. Each stage keeps its own hooks. Collection does
not require an artifact, because its result is the recorded items.

A workflow may contain at most one `items` step, at any nesting level. This is
an intentional limitation: collected items belong to the workflow run, and ww
expands every per-item stage in one place when collection completes. An `items`
step cannot also declare `steps`, `loop`, an item operation marker, or child
tasks. The former `steps_per_item` key is not accepted.

## Handlers

Each root handler, and each step through the same shared shape, accepts:

| Key | Type | Required | Meaning |
| --- | --- | --- | --- |
| `name` | name | yes* | Identifier; key omitted in shorthand or for an inline hook command. |
| `description` | string | no | Instruction or explanation. |
| `skill` | `true` | no | Require a discovered agent skill with this name. |
| `slash_command` | `true` | no | Require a discovered slash command with this name. |
| `prompt` | `true` | no | Explicitly select plain agent work instead of skill or slash-command resolution. |
| `mcp` | non-empty string | no | MCP connection; `description` supplies its work instruction. |
| `argv` | string list | no | Automatic argument-vector action run by `ww`. |
| `shell` | string | no | Automatic shell action run by `ww`. |
| `args` | string list | no | Positional arguments for `shell`. |
| `env` | string mapping | no | Environment values for `shell`. |
| `assert` | assertion | no | Expected command output. |
| `idempotent` | boolean | no | Running the command action again is harmless: an interrupted run is replayed by `next` instead of waiting for an operator. Requires `argv`, `shell`, or `command`. Defaults to `false`. |
| `provide` | list of provided values | no | Values requested from the agent and exposed to later actions. |
| `update_metadata` | list of metadata values | no | Values saved by an agent-owned action. |
| `update_document` | list of document updates | no | Documents an agent-owned action creates or edits in place; each names a root document, with the text instructing the update. |
| `agent` | non-empty string other than `auto` | no | Preferred executor guidance. |
| `model` | non-empty string | no | Model guidance; `auto` stops inheritance. |
| `reasoning` | non-empty string | no | Reasoning guidance for this action. |

### Named-entry shorthand

Workflows, root handlers, steps, and hook handlers may put the name in the first mapping
key and its optional description in the value:

```yaml
handlers:
  - update-yaml-specification: Update specification.md.
  - no-description: ~

workflows:
  - task: The standard development workflow.
    steps:
      - develop: Implement and test the change.
```

The value must be a string or YAML `null` (`~` or an empty value). Null means
that the entry has no description. This is parsed exactly like the corresponding
long form before validation and plan construction. If `name` is present, the
mapping always uses the existing long form and unknown keys remain errors.

An action uses one form: `skill: true`, `slash_command: true`, `prompt: true`,
`mcp`, `argv`, or `shell`. These forms cannot be combined. `prompt` accepts
only `true` and explicitly selects plain agent work. With no explicit action,
`ww` resolves the name as a project skill,
slash command, or ordinary agent prompt, in that order.

`provide` entries accept `name` (required) and `description` (optional). They
also accept the named-entry shorthand, where the first key is the provided
value name and its string or null value is the description:

```yaml
provide:
  - workflow: The workflow name corresponding to one of {{__workflows}}.
  - reason: ~
```

Names must be unique in the list and cannot start with `__` or replace a core
variable such as `__task_workspace_dir`.

### Assessments

`assess` asks the agent to choose a named outcome before work continues. Prefer
`positive` or `negative` when the evidence supports either; reserve `mixed` for
material uncertainty. Select the result with `next --outcome <label>`.

```yaml
- assess:
    question: Does recent development warrant refactoring?
    outcomes:
      positive:
        handler: refactor-plan
      negative:
        steps:
          - record: No refactoring is needed now.
      mixed:
        steps:
          - investigate: Gather the missing evidence.
```

Each outcome is one ordinary step shape: a direct handler/action, `handler`,
`handlers`, or `steps`. These work types remain mutually exclusive. After the
chosen outcome's work, the workflow continues with the step after `assess`.
An outcome may instead be `stop_workflow: true`, alone: choosing it completes
the workflow, skipping everything after it, completion hooks included. At
least one outcome must have work. The standard answers `positive`, `negative`,
and `mixed` are always accepted: one the assessment does not declare runs
nothing and continues after the assessment, so a gate declares only the outcome
that has work. Any other label must be declared. The compact form, `- assess: <question>`,
accepts `positive` or `negative`; positive continues to the next step and
negative completes the workflow, like an outcome with `stop_workflow: true`.

```yaml
- assess:
    question: Were conflicts resolved in non-trivial code?
    outcomes:
      positive:
        steps:
          - review: Review the resolutions.
      negative:
        stop_workflow: true
```

The assessment's page lists every outcome with what it does, and the page
after it offers one `next --outcome <label>` command per outcome.

`update_metadata` entries accept:

| Key | Type | Required | Meaning |
| --- | --- | --- | --- |
| `name` | name | yes* | Name used with `ww complete --metadata`; key omitted in shorthand. |
| `key` | dotted path | yes | Storage path, for example `jira.issue_id`. |
| `description` | string | no | Instruction for producing the value. |
| `scope` | `task` or `project` | no | Storage scope; default `task`. |
| `append` | boolean | no | The key holds a list. Each completion may pass the name once per value, or omit it; values are appended to what is stored and repeats are dropped. Default `false`. |

The named-entry shorthand is supported here too; `key` remains required and
`scope` keeps its default:

```yaml
update_metadata:
  - last_auto_refactored_at: Save the current timestamp in YYYY-MM-DD HH:mm format.
    key: last_auto_refactored_at
    scope: project
```

Within one action, metadata names and `(scope, key)` pairs must be unique, and
paths in the same scope cannot overlap (for example `jira` and
`jira.issue_id`). Only agent-owned actions can save metadata.

## Commands

`argv` and `shell` are root handler keys:

```yaml
name: commit
argv: [git, commit, -m, "{{message}}"]
```

An action uses exactly one form:

| Form | Allowed keys | Rules |
| --- | --- | --- |
| Argument vector | `argv` | Non-empty list of non-empty strings. |
| Shell | `shell`, `args`, `env` | Non-empty shell source; optional string-list `args` and string-valued `env` mapping. |

Templates are allowed in `argv`, shell `args`, and shell `env` values. They are
not allowed in shell source; pass dynamic values through `args` or `env`.

To assert command output, add `assert` beside the action:

```yaml
argv: [git, status, --porcelain]
assert:
  operator: eq
  expected: clean
```

`assert` accepts only `operator: eq` and a non-empty string `expected`.

## Hooks

A hooks mapping accepts these lifecycle phases, each containing a list:

- `before_start_workflow`
- `before_in_progress`
- `before_complete`
- `after_complete`
- `before_complete_workflow`

Each hook entry accepts one handler form and optional filters:

| Key | Type | Availability |
| --- | --- | --- |
| handler keys | shared handler shape | All scopes; used directly for a singular hook. |
| `handlers` | non-empty list of handler mappings | All scopes; used only for multiple actions. |
| `steps` | list of step names or paths | Global and workflow step-lifecycle hooks only; unavailable to workflow-boundary hooks. |
| `workflows` | list of names/references | Global hooks only. |

Filters must refer to configured workflows or effective steps; the reserved
`init` step is always an effective step. A bare step name such as `fix` matches
that name at any nesting level unless the same selector is also an exact logical
path. Exact paths take precedence, so `steps: [code-review]` selects a top-level
`code-review` wrapper rather than a nested `code-review/code-review` step. Use a
slash-separated path such as `plan-and-fix/fix` to target one specific substep.
For per-item stages, omit the runtime item segment and use the logical path, for
example `review/fix`.
`before_start_workflow` and `before_complete_workflow` are workflow boundaries:
they run once per workflow in global then workflow order and cannot be declared
at step scope. The completion boundary follows every step's `after_complete`
hooks and precedes the built-in workflow summary and any handoff transition.
The other phases run in global, workflow, then step order for each matching step.

For one action, put the shared [handler keys](#handlers) directly on the hook.
A name-only handler references the root catalog; action keys define an inline
handler. Use `handlers` to apply the same filters to multiple ordered handlers.
A hook runs a single action, so it cannot reference a root handler that defines
`loop`, `steps`, or `items`; use such a handler as a workflow step instead.
Every item has exactly the same shape:

```yaml
hooks:
  before_complete:
    - workflows: [task, bugfix]
      steps: [develop]
      handlers:
        - name: update-architecture-documentation
        - name: update-readme
        - name: publish-documentation
          mcp: github
          description: Publish the updated documentation.
```

Grouped and singular hook handlers accept the shorthand too. Hook filters are
not considered handler names, so a singular filtered hook can be written as:

```yaml
hooks:
  before_start_workflow:
    - handlers:
        - ext/ww/git/handlers:is-git-clean: ~
        - ext/ww/git/handlers:start-task-branch: ~
    - check-environment: ~
```

A `handlers` group cannot contain handler action keys at its own level; entries
are mappings and the list cannot be empty. A singular hook can instead declare
a workflow transition with `workflow` plus optional `description`, `agent`,
`model`, and `reasoning`:

```yaml
hooks:
  after_complete:
    - workflow: "{{next_workflow}}"
```

## Minimal example

```yaml
handlers:
  - name: test
    argv: [python, -m, pytest, -q]

hooks:
  before_complete_workflow:
    - workflows: [task]
      name: test

workflows:
  - name: task
    steps:
      - name: develop
        description: Implement and verify the requested change.
```

Use `ww-agentic-workflows lint` to validate the complete configuration. Use
`ww-agentic-workflows plan --workflow <name> --agent <agent>` to inspect a
selected workflow's resulting execution plan.
