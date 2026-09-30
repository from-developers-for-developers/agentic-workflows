# `../ww-agentic-workflows.yaml` specification

`../ww-agentic-workflows.yaml` defines what `ww` workflows do. The file is strict: unknown
keys, invalid types, and invalid references are errors.
The former name `workflows.yaml` is not read; ww stops when it finds that file
and `init` renames it.

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
| `extends` | boolean | no | `false` makes this file's level ignore the levels above it; see [Configuration levels](#configuration-levels). Defaults to `true`. |
| `imports` | list of file paths | no | Other YAML files composed into this one; see [Imports](#imports). Must come before every key but `extends`. |
| `workflows` | list of workflows | yes | At least one workflow is required in the composed configuration: in this file, an imported one, or another level. |
| `modes` | list of modes | no | Reusable agent guidance. |
| `profiles` | mapping | no | Named agent profiles. |
| `documents` | list of documents | no | Durable, free-format files that workflows read and update across runs. |
| `handlers` | list of handlers | no | Reusable actions referenced by hooks. |
| `hooks` | hooks mapping | no | Hooks applying across workflows. |
| `rules` | mapping of rule groups | no | Named groups of rule files, active where their filters allow; see [Rules](#rules). |

The generated task ID format, `task_format`, is not a key of this file: it
lives in `ww-agentic-workflows.json` (see the features guide), and a
`task_format` key in any YAML file, at any level or in an import, is an error
naming that file.

The legacy root key `tasks` is rejected.

## Imports

A root file — `../ww-agentic-workflows.yaml` or the root file of another
[configuration level](#configuration-levels) — may split its definitions across
other YAML files by listing them under `imports`, which must come before every
other key except `extends`:

```yaml
imports:
  - workflows/handlers.yaml
  - workflows/review.yaml

workflows:
  - task: The standard development workflow.
    steps:
      - develop: Implement the change.
      - code-review: ~
```

- Each entry is a non-empty path, relative to the directory of the file that
  lists it, to an existing file that contains a mapping. Only `imports` resolve
  this way: every other relative path in any file, such as a document `path`,
  a handler's `argv`, or the settings' `projects` and `worktree_dir`, still
  resolves against the project root.
- An imported file may define every root key above except `imports`: imports do
  not nest, so every imported file is listed in its level's root file. A file
  may not be listed twice, may not be one of the level root files, and a root
  file may not import itself.
- Definitions fold in list order, and the root file last. A later file
  overrides an earlier one, so a root file overrides every import it lists:
  - an entry of `workflows`, `modes`, `documents`, or `handlers`, and a
    `profiles` entry, replaces the entry of the same name from an earlier file,
    in that entry's original position;
  - `hooks` entries carry no name, so each phase's entries from a later file
    run after those from earlier files;
  - any other key takes the later file's value.
- Overriding across files is not an error. `lint` prints one notice per
  overridden definition, naming the file that defined it and the file that
  overrode it. A name repeated within a single file is still reported as a
  duplicate.
- The files are composed in memory, on every command, into one document that is
  then read exactly as a single `ww-agentic-workflows.yaml`; nothing is cached on disk.
  Every rule in this specification applies to that composed document.

## Configuration levels

Workflows come from up to three levels, applied top to bottom:

| Level | Root file | Required |
| --- | --- | --- |
| machine | `ww-agentic-workflows.machine.yaml` in `$WW_MACHINE_CONFIG_DIR`, else `$XDG_CONFIG_HOME/ww-agentic-workflows/`, else `~/.config/ww-agentic-workflows/` | no |
| repo | `../ww-agentic-workflows.yaml` | yes |
| local | `../ww-agentic-workflows.local.yaml`, next to the repo file | no |

The repo file is what makes a directory a ww project; a machine file alone
never does. A level is its root file plus the files that root imports, and
each level may use `imports` as described above.

- Levels fold in order, machine first, each level's imports before its root
  file, with the same rules as imports: a lower level overrides the levels
  above it, named entries are replaced one by one, `hooks` entries of each phase
  are added after those from above, and any other key takes the lower level's
  value.
- `extends` is a boolean, `true` by default. When any file of a level — its
  root or one of its imports — sets `extends: false`, that level ignores every
  level above it and folding starts again there. `extends: true` changes
  nothing. `lint` reports each file left out as a notice.
- `lint` lists the configuration files it read, and `plan` ends with the same
  list; `lint` notices name the file of the level that overrode a definition.

```yaml
# ~/.config/ww-agentic-workflows/ww-agentic-workflows.machine.yaml
handlers:
  - name: test
    argv: [pytest]
```

```yaml
# ww-agentic-workflows.local.yaml
modes:
  - economy: Keep answers short.
```

`ww-agentic-workflows.json` has matching `.machine.json` and `.local.json`
levels, which are always deep-merged and take no `extends` key; `task_format`
is one of its keys, so a lower JSON level replaces it. See the features guide.

Projects, the directories a task may work in, are configured in
`ww-agentic-workflows.json` rather than here because their locations differ per
machine; see the features guide.

## Modes and profiles

A mode has these keys:

| Key | Type | Required |
| --- | --- | --- |
| `name` | name | yes |
| `description` | string or list of non-empty strings | no |
| `workflows` | `"*"` or list of workflow names | no |
| `steps` | `"*"` or list of step names or paths | no |

Modes accept the named-entry shorthand too: `- economy: Use fewer tokens.` is
equivalent to `{name: economy, description: Use fewer tokens.}`. A null value
omits the description. The filters may sit beside the shorthand:
`{economy: Use fewer tokens., steps: [develop]}`.

A mode applies to a run when it is selected: `start --mode` names it, or,
without `--mode`, the workflow lists it in its `modes`. A mode with
`workflows` or `steps` (or both) is **automatic**: it also applies, without
being selected, to every step its filters admit, matched like a hook's
filters; a workflow's heirs follow it. `--mode` replaces only the workflow's
default modes and never removes an automatic mode. See
[Workflow and step filters](#workflow-and-step-filters) for the forms.

Every agent step's page, including item stages and loop steps, lists its
modes in a **Modes** section, each with its description: the selected modes
first, then the automatic modes that admit the step, a mode both selected and
automatic once. The same pages get modes as get rules: not `init`, hooks,
verification items, or the built-in workflow summary. The modes are resolved
per step when the run starts and frozen in its plan snapshot, like rules.

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
| `role` | `manager` or `worker` | no | The role every step inherits unless it or an enclosing step sets its own; see the step key. |
| `subagents` | boolean | no | `false`: no step's performer spawns subagents, unless a step sets `true`; see the step key. |
| `handoff` | — | — | Removed; rejected with a message. A workflow transition (`workflow` on the last step) makes a handoff workflow; see the step key `workflow`. |
| `runtime` | `single` or `auto` | no | The runtime `start` uses for this workflow when `--runtime` is omitted; it outranks the project default in `ww-agentic-workflows.json`, and the flag outranks it. |
| `restartable` | boolean | no | A new `start` of this workflow while its previous run is unfinished abandons that run and opens a new one; the abandoned run stays in the task's history. Without it, a task with an unfinished run refuses another start. An unfinished run of a different workflow is never abandoned this way. Defaults to `false`. |
| `inherit` | workflow name | no | Copy that workflow completely: steps, workflow hooks, and every setting. The workflow's own keys other than `steps` and `hooks`, which it may not declare, replace the copied values. A global hook filtered to the inherited workflow also runs for this one. Chains are allowed; a cycle or unknown name is an error. |
| `recommended_next_workflow` | workflow name or null | no | Offered to the operator when a run completes: the page asks through the agent's choice menu and shows the `start` command for the same task, to run only on confirmation. Inherited like any setting; `null` clears an inherited one. Invalid in a handoff workflow (one with a workflow transition). |

Execution settings inherit from workflow to enclosing steps to the current
step. Hooks then apply the referenced root handler and the invocation override.
Omission inherits; explicit `model: auto` or `reasoning: auto` stops inheritance.
Model and reasoning are coupled: when a child changes the effective model and
omits reasoning, reasoning resets to `auto`. Repeating the same model preserves
the inherited reasoning. `agent` falls back to the agent passed to `plan` or
`start` and may not be `auto`.

`../ww-agentic-workflows.json` sets ww-wide project behavior separately from workflow
syntax. `enabled` is `true` (the default: agents use ww for project work),
`false` (agents do not use ww, `discover` says only that, and `start` refuses),
or `"on_request"` (ww stays available, but agents use it only when the user
explicitly asks for it; `discover` says so before its full catalog and reports
`"enabled": "on_request"` in JSON); any other value is an error naming the three.
`loop_max_times` is a positive integer and defaults to `3`. The file may
also override the internal requests of the implicit init action, `cheapest` /
`low`, and of the workflow-summary action, `auto` / `auto`. `workflows` switches
off the workflows ww provides to every project, currently only `catchall`; each
entry is an object whose only key, `enabled`, defaults to `true`. A workflow of
the same name in `ww-agentic-workflows.yaml` replaces the provided one instead.
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
| `rules` | list of rule entries | The step's own rules and the rule groups it names; see [Step rules](#step-rules). Not allowed on a step without agent work of its own, such as a container or a command. |
| `profile` | profile value | Overrides the profile inherited from the workflow and every enclosing step. Nested steps, loop bodies, and per-item stages inherit it in turn. |
| `role` | `manager` or `worker` | Who performs the step. `manager` keeps it in the managing session in every runtime and ignores its profile, agent, model, and reasoning settings; `worker`, the default, lets an `auto` run delegate it. Inherited from the workflow and every enclosing step, like `profile`; nested steps, loop bodies, and per-item stages inherit it in turn. Only agent steps take it: on a step ww runs, such as a command, it is an error. |
| `subagents` | boolean | When `false`, whoever performs the step, the manager or a worker, does all of its work alone and spawns no subagent for anything; the step's page says so. It says nothing about who performs the step (`role`) or with which model. Inherited like `profile`; a nested step may set `true` again. Defaults to `true`. |
| `interactive` | boolean | The step is a conversation with the operator, held by the session that can talk to them; it implies `role: manager`, and `role: worker` beside it is an error. Its completion is refused until the conversation was recorded with `interact` and ended. Defaults to `false`. |
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
| `depends_on` | name | Earlier artifact-producing step whose artifact is supplied to this step: an earlier sibling, or an earlier step of an enclosing level, the nearest one first. Inside assessment outcomes the assessment itself is eligible, and inside per-item stages the `items` step; an enclosing loop or plain group is not. |
| `items` | `null`, string, or mapping | Collects work items, then runs per-item stages for each; see [Items](#items). |
| `workflow` | workflow name or `{{variable}}` | Makes the workflow a handoff workflow and ends it by starting that workflow as the task's next run. A workflow has at most one transition, and nothing may follow it: valid only on the last top-level step (with no completion hook applying to it), with `description`, `agent`, `model`, and `reasoning` at most; or on a hook, see [Hooks](#hooks). |
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
`../ww-agentic-workflows.json` when the wrapper omits it, and is frozen in the saved
workflow plan. At the limit, ww does not expose a continuation command: it
reports `awaiting_operator` with `operator_reason: loop_limit` and a warning
that must be escalated to the user for manual resolution, and shows the operator's exit, `next --force --force-reason`,
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

An extension handler reference can be used directly as a step, for
example `- ext/ww/git/handlers:is-git-clean: ~`. It is resolved and validated
when the workflow plan is compiled, just like an extension handler in a hook.
Such an entry, as a step or as a hook, may carry `workdir` and no other key;
everything else about the handler is the extension's to define.

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
| `role` | `manager` or `worker` | `manager` performs the per-item stages in the managing session. |
| `subagents` | boolean | `false`: the stages' performers spawn no subagents. |

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
`profile`, `role`, and `subagents` apply to collection and are inherited by the stages;
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
or that is the manager's (`role: manager`), starts a new assignment; the span resumes
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
| `workdir` | `task`, `project`, or `root` | no | The directory this action works in; see [Working directory](#working-directory). Defaults to `task`. |

### Working directory

`workdir` chooses the directory an action works in:

- `task` (the default): the task workspace, which is the selected Git worktree
  when an extension chose one, otherwise the `--project` directory, otherwise
  the project root.
- `project`: the `--project` directory's own checkout, never a worktree made
  from it; the project root for a task started without `--project`.
- `root`: the project root, which holds the configuration and `.ww`.

On a step, the value applies to the step's instruction, whose working-directory
`cd` names that directory, to its `argv` or `shell` action, and to
`{{__task_workspace_dir}}`, which resolves to that directory for the step.
Nested steps, loop bodies, per-item stages, and an assessment inherit it from
the enclosing step unless they set their own. A step that copies a root
handler with `handler` takes the handler's `workdir` unless it sets its own.

A hook does not inherit its step's `workdir`. A hook entry's own `workdir`
applies, otherwise that of the root handler it names, otherwise `task`:

```yaml
handlers:
  - name: refresh-shared-config
    argv: [make, shared-config]
    workdir: root

workflows:
  - name: task
    steps:
      - update-shared-notes: Update the git-ignored notes in {{__task_workspace_dir}}.
        workdir: root
        hooks:
          after_complete:
            - refresh-shared-config: ~
            - name: lint
              argv: [make, lint]
              workdir: project
```

ww gives a directory outside the task workspace no special Git treatment:
changes made there are not committed by the task and remain for the operator.

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

`assert` accepts `operator: eq` with a non-empty string `expected`, which
the whole output must equal, or `operator: empty`, which takes no `expected`
and requires the output to be empty or whitespace:

```yaml
shell: grep -l TODO $WW_STEP_CHANGED_FILES || true
assert: { operator: empty }
```

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
| `steps` | `"*"` or list of step names or paths | Global and workflow step-lifecycle hooks only; unavailable to workflow-boundary hooks. |
| `workflows` | `"*"` or list of workflow names | Global hooks only. |
| `on_failure` | `fix` or `operator` | `before_complete` hooks only, and not on a workflow transition. `fix` makes the hook a check of the step: a failure rejects the step's completion and returns the step to its worker; see [Rules](#rules). Default `operator`: a failure stops the task for the operator. Also accepted on each member of `handlers`, which inherits the group's value. |

The filter forms are described in
[Workflow and step filters](#workflow-and-step-filters).
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
`model`, and `reasoning`. A transition hook belongs only on the
`after_complete` hooks of a workflow's last top-level step, as their last
entry, since nothing runs after a handoff; at global or workflow scope it is
rejected:

```yaml
steps:
  - choose: Choose the next workflow.
    provide:
      - next_workflow: The workflow to run next.
    hooks:
      after_complete:
        - workflow: "{{next_workflow}}"
```

### Workflow and step filters

Every construct that filters by workflow or step, hooks, rule groups and
modes, takes the same two keys, `workflows` and `steps`, each either the
string `"*"` (all) or a list of names. Only where a construct may carry a
filter differs.

| Form | Hook | Rule group | Mode |
| --- | --- | --- | --- |
| omitted | every workflow or step | every workflow or step | every workflow or step if the other key is set; with neither key the mode is not automatic |
| `"*"` | every workflow or step | every workflow or step | every workflow or step |
| `[a, b]` | only those names | only those names | only those names |
| `[]` | every workflow or step, like omission | none: the group applies only where a step names it | every workflow or step, as on a hook |

`"*"` is never redundant, even where omission already means all. `"*"` inside
a list (`["*", task]`) is an error, and so is any other string: a bare name is
not a list, write `[task]`.

`ww rules --json` renders a filter that admits all as `"*"`.

## Rules

A rule is a sentence a step's agent follows while working. It may carry a
command ww runs when the step completes, a **check**; a failed check rejects
the completion and sends the step back to its worker, up to `max_fixes` times.
Rules are delivered on the page of every agent step they apply to: not `init`,
hooks, or the built-in workflow summary.

### Rule files

A rule file is Markdown: optional YAML frontmatter between a first line
`---` and the next `---`, then the rule's text, which must not be empty. The
text's first sentence is its summary on the step page.

```markdown
---
paths: ["src/**/*.php"]
check:
  shell: find $WW_STEP_CHANGED_FILES -name '*Service.php' -not -path 'src/Service/*'
  assert: { operator: empty }
max_fixes: 5
model: claude-opus-5-5
---
Put every `*Service.php` under `src/Service/<Domain>/`, one class per file.

Controllers must not instantiate services; inject them.
```

| Key | Type | Meaning |
| --- | --- | --- |
| `paths` | non-empty list of globs | The files the rule is about, relative to the step's directory. `*` and `?` stay within a path segment, `**` spans segments, and a glob without `/` matches a file name anywhere. A check whose globs match no changed file does not run. |
| `check` | command | `argv`, or `shell` with `args` and `env`, and optional `assert`, as in [Commands](#commands); `command` and `idempotent` are not accepted. |
| `max_fixes` | positive integer | Rejections this check allows; defaults to `max_fixes` in `ww-agentic-workflows.json` (3). |
| `agent`, `model`, `reasoning` | string | The worker that verifies the rule; see [Verifying rules without a command](#verifying-rules-without-a-command). |

A rule's identity by wording is the SHA-256 of its text with surrounding
whitespace removed and runs of whitespace collapsed to one space.

### Rule groups

The root `rules` maps group names to their items, as a list or as a mapping:

```yaml
rules:
  php-architecture: [rules/php/]
  docs-style:
    rules: [rules/docs/]
    workflows: [task, bugfix]
    steps: [develop, refactor]
    reasoning: high
  engineering: [php-architecture, docs-style]
```

| Key | Type | Meaning |
| --- | --- | --- |
| `rules` | non-empty list of strings | Each is a group name, else a path relative to the file that declares it: a directory contributes every `*.md` directly inside it, sorted by name; a file, that rule. |
| `workflows` | `"*"` or list of workflow names | The workflows the group applies to; omitted or `"*"`, every workflow, and a workflow's heirs follow it. |
| `steps` | `"*"` or list of step names or paths | The steps it applies to, matched like a hook's `steps`; omitted or `"*"`, every step. `steps: []` applies nowhere on its own: only a step naming the group gets it. See [Workflow and step filters](#workflow-and-step-filters). |
| `agent`, `model`, `reasoning` | string | Defaults for the group's rules; a rule file's own value wins. |

A rule's ID is `<group>/<file stem>`. Two files with the same stem in one group
are an error; a file listed in two groups has an ID in each. A group naming
another includes its rules under that group's IDs; a cycle is an error, as is
an item that names no group and no existing path. An absolute path is accepted
and `lint` reports it as a notice. A later configuration level or import
replaces a group of the same name as a whole.

A configured extension may ship rule groups; they come before the YAML groups,
and a name declared in both is an error.

### Step rules

A step's `rules` is a list. A string names a group, which then applies to the
step whatever its filters say; else a rule file or directory that exists
relative to the declaring file; else it is the literal text of a rule. A string
shaped like a reference, letters, digits, `_`, `.`, `-` with at most one `/`
and no trailing punctuation, that names neither a group nor a file is an error.
A mapping defines a rule of the step's own:

| Key | Type | Meaning |
| --- | --- | --- |
| `text` | string | The rule. |
| `argv`, `shell`, `args`, `env`, `assert` | command | Its check; a command without `text` is a pure check, summarised by the command. |
| `max_fixes`, `agent`, `model`, `reasoning` | | As in a rule file. |

One of `text` or a command is required. The step's own rules have IDs
`<step>/<position>`, counted from 1, and `<step>/<file stem>` for a file or
directory entry; a group's rules keep their group IDs.

```yaml
steps:
  - name: develop
    description: Implement it.
    rules:
      - Keep the public CLI unchanged.
      - text: Include "foo" in every file you change.
        shell: grep -L foo $WW_STEP_CHANGED_FILES || true
        assert: { operator: empty }
      - argv: [vendor/bin/phpstan, analyse]
      - rules/one-off/no-migrations.md
      - php-architecture
```

A step receives, in order, the root groups whose filters admit it, then its
own entries; a rule ID reached twice counts once. Its checks are its rules'
commands in that order, then its `before_complete` hooks with
`on_failure: fix`, named `<step>/<handler name>`, or `<step>/<program>` for an
inline command. Such a hook must run a command that asks the agent for no
values; it runs in the step's directory and is not also run as a hook. On a
step with no agent work of its own, it runs as an ordinary hook.

### Running checks

When the step's worker runs `complete`, ww runs the step's checks before
recording anything. Each sees `WW_STEP_CHANGED_FILES`: the files the step
changed, newline-separated and relative to the step's directory, narrowed to
the check's `paths`. A check fails on a non-zero exit or a failed assertion.
In a shell check, a bare `$WW_STEP_CHANGED_FILES` splits on whitespace, so a
path containing a space needs `printf '%s\n' "$WW_STEP_CHANGED_FILES" | xargs -d '\n'`;
an `argv` check receives the variable in its environment only.

### Verifying rules without a command

A rule without a check of its own is never judged by the worker who did the
step. When that worker completes and the step's checks pass, ww holds the
completion, records nothing yet, and inserts **verification items** right
before the step: agent items ww generates, IDs
`<workflow>:<step path>:verify:<n>`, one per distinct worker among the rules
(the rule's or group's `agent`, `model`, `reasoning`, else the step's). Each is
an assignment of its own, and asks about each of its rules according to the
rule-automation store:

| The store has the rule | The verifier is asked |
| --- | --- |
| nothing, or `interpreted` | `unresolved`: an interpretation and an approach, or `not-convertible`, or `ambiguous` |
| `approach-approved` | to prepare and prove its check, or report `not-convertible` |
| `rejected`, `not-convertible`, or an undecided proposal from elsewhere | `judged`: a verdict |

A rule whose wording has a `converted` check is checked by it and asks no
verifier. What the store says is read when the step begins and kept with it.

A verification item completes with its findings as `--artifact` and one
`--rule-result` per rule, plus one `--check-result` per check it prepared;
both are repeatable JSON objects, and `complete` refuses a missing, unknown,
duplicate, or malformed one, and either option on any other step.

| `--rule-result` key | Meaning |
| --- | --- |
| `id` | The rule ID; required. |
| `status` | `approach`, `not-convertible`, `ambiguous` (not for a judged rule), or `judged` (only for one). |
| `interpretation` | The rule in one sentence. |
| `check`, `approach` | For `approach`: the kebab-case check name, at most 40 characters, to create or extend, and for an unresolved rule the approach in one line. |
| `reason` | For `not-convertible`: why. |
| `candidates` | For `ambiguous`: two or more readings. |
| `verdict`, `failures` | For `judged` and `not-convertible`: `pass`, or `fail` with `failures`, each `{file, line?, what}`. |

| `--check-result` key | Meaning |
| --- | --- |
| `name` | The check a prepared rule names; required. |
| `argv`, or `shell` with `args` and `env`, and `assert` | Its command, as in [Commands](#commands). |
| `config` | The project files holding the check's logic. |
| `covers` | The IDs of the step's rules it checks, including every rule naming it. |
| `proven` | Whether it failed on a deliberate violation and passed on the change. |

A failing verdict rejects the held completion as a failed check would, under
the rule's `max_fixes`. Proposals stop the task with `operator_reason:
check_proposed`; the operator answers with `next`:

| Option | Effect |
| --- | --- |
| `--approve <hash or check>` | Approves a rule's approach, so a verifier prepares its check; or approves a check, converting it and the rules it covers, or its pending revision. Repeatable; the CLI prints the command and asks first. |
| `--approach <hash> "<text>"` | Approves the operator's own approach instead. Repeatable. |
| `--pick <hash>=<number>` | Fixes an ambiguous rule's reading; a verifier proposes an approach for it again. Repeatable. |
| `--force --force-reason "<why>"` | Rejects every undecided proposal; those rules are judged. |

A rule hash may be given by a unique prefix of at least 8 characters. Once
nothing is undecided, ww runs the step's checks again, newly approved ones
included, then verifies what remains or records the held completion.

#### Who approves: `rules.approval`

`ww-agentic-workflows.json` sets who approves what verifiers propose:

```json
"rules": { "approval": "operator" }
```

| Value | Approaches | Checks | Stops per converted rule |
| --- | --- | --- | --- |
| `operator` (default) | the operator | the operator | 2 |
| `check` | automatic | the operator | 1 |
| `auto` | automatic | automatic, only when `proven: true` | 0 |

`rules` takes only `approval`; another key or value is an error. The setting
is read when a verification item completes, not frozen into the run. ww's
approvals go through the same code as the operator's `next --approve`, are
recorded with `approved_by: auto`, and an approved check runs on the held
completion at once. Under `check`, an ambiguous rule and a proposed check
still stop the task. Under `auto` nothing stops it: an unproven check stays
`proposed` and an ambiguous rule stays `ambiguous` in the store, and meanwhile
a verifier judges their rules, as for an undecided proposal from elsewhere;
`not-convertible` is recorded as always.

#### Rules converted in this run

When a run completes, ww lists what it converted, from the store entries whose
`approved_in` is the run: for each check its status, who approved it, the rule
IDs it covers with a wording summary, its command, config files, and whether it
was proven, and for an automatic approval the undo command, `rules revoke
<check>`. Under `auto` the run's proposals left undecided follow. The section
is shown on the completion page (JSON: `rule_conversions`) and appended by ww
to the workflow summary artifact as `## Rules converted in this run`, in every
approval mode, whenever it is not empty. No agent writes it.

### The rule-automation store

`ww-rule-automation.json` at the project root keeps what verification
learned. It is meant to be committed; ww writes it under its own lock, and
leaves it out of every change set. Verification never edits YAML or rule
files; only the operator's `rules` write commands do.

```json
{
  "schema_version": 2,
  "rules": {
    "9f2a…": {
      "text": "Controllers must not instantiate services; inject them.",
      "status": "converted",
      "interpretation": "No `new *Service(` in src/Controller.",
      "approach": "deptrac layer rule",
      "check": "deptrac",
      "proposed_in": "task:develop:verify:1",
      "proposed_run": "TASK-7/01-task",
      "approved_by": "operator",
      "approved_in": "TASK-7/01-task"
    }
  },
  "checks": {
    "deptrac": {
      "argv": ["vendor/bin/deptrac", "analyse", "--no-progress"],
      "assert": null,
      "config": ["deptrac.yaml"],
      "covers": ["9f2a…"],
      "proven": true,
      "status": "converted",
      "proposed_at": "2026-09-29T10:00:00Z",
      "approved_at": "2026-09-29T11:00:00Z",
      "proposed_in": "task:develop:verify:2",
      "proposed_run": "TASK-7/01-task",
      "approved_by": "operator",
      "approved_in": "TASK-7/01-task"
    }
  }
}
```

`rules` is keyed by the rule's text hash; its `status` is one of
`approach-proposed`, `approach-approved`, `interpreted`, `proposed`,
`converted`, `rejected`, `not-convertible`, `ambiguous`, with `reason` for a
rejected or unconvertible rule and `candidates` for an ambiguous one. `checks`
is keyed by check name; `status` is `proposed`, `converted`, or `rejected`,
`covers` lists rule hashes, and `pending` holds a proposed revision of a
converted check, and `reason` explains a rejected one. Only a `converted`
check runs. On both maps, `proposed_in` is the verification item that last
reported on the entry and `proposed_run` its run, as `<task>/<run>`;
`approved_by` (`operator` or `auto`) and `approved_in` (`<task>/<run>`) record
who approved an approach, a picked reading, or a check, and in which run.
Version 1 of the store, without these fields, is still read, its approvals
with an unknown approver; ww writes version 2. An unknown key, status, or
`schema_version` is an error.

### Rule commands

| Command | Effect |
| --- | --- |
| `check <task> [--json]` | Runs the checks of the step in progress against its change set so far, exactly as `complete` would, and prints the failures in the fix page's shape, or `All checks pass`, plus the rules a verifier judges at completion. Records nothing: no attempt counts and no output is kept. Exits 1 when a check fails. Not written to the audit log. |
| `dispute <task> --rule <id> --reason "<why>"` | Only while the step is in progress, and only for an ID a rejected completion of it failed: a rule, a `fix` hook, a derived check, or a judged rule. Stops the task with `operator_reason: check_disputed`; the page shows the check's text, command, and last output, and the worker's reason. |
| `rule <task> <id> [--json]` | One rule or check of the task as its plan froze it: full text, globs, rule file (or the step's own list), command and assertion, `max_fixes`, the steps of the task that carry it, and for a rule without a command what the rule-automation store knows about its wording. |
| `rules [--json]` | The declared root groups with their filters, verifier hints, and rules (ID, summary, globs, whether it has a check, file, times disputed), then each step's own rules and the groups it names. |
| `rules revoke <check> [--reason "<why>"] [--yes] [--json]` | Shows a `converted` or `proposed` store check, asks, and rejects it, recording the reason, together with the rules whose entries name it, which a verifier judges from then on. Never touches YAML, rule files, or the check's config files; the output says they stay for the operator. `--yes` skips the question; without it and without a terminal, it refuses. |
| `rules prune [--yes] [--json]` | Lists the store's orphans, rule entries whose wording no declared rule has and checks that cover only such rules and that no remaining rule names, asks, and deletes them. `--yes` skips the question. |
| `rules add <group> --text "<text>" [--paths <glob>...] [--check-shell "<sh>" \| --check-argv <arg>...] [--assert empty\|eq:<value>] [--id <stem>]` | Creates `<stem>.md` in the group's first directory item; the stem is the first five words of the first sentence in kebab-case unless `--id` gives one. Refuses an existing file, a group without a directory, and a group an extension ships. Reports each glob's match count among the project's files. |
| `rules add --group <name> --dir <path> [--workflows <name>...] [--steps <name>...]` | `<path>` is relative to the project root and inside it. Adds the group `{rules: [<path>/], workflows, steps}` to `ww-rules.yaml` and, the first time, `ww-rules.yaml` to the repo file's `imports`; creates the directory. A filter option without a name writes `[]`; `'*'` alone writes `"*"`. |
| `rules edit <id> [--text "<text>"] [--paths <glob>...]` | Replaces a rule file's body, its `paths`, or both, keeping every other byte; warns when the wording's hash changes and names the store entry and approved check that stop matching. Refuses a rule written in a step's `rules` list. |
| `rules move <id> <group>` | Moves the rule file unchanged into the group's first directory; the rule's ID becomes `<group>/<stem>`. |
| `rules filter <group> [--workflows <name>...] [--steps <name>...] [--all-workflows] [--all-steps]` | Sets a `ww-rules.yaml` group's filters; `--workflows '*'` / `--steps '*'` writes `"*"`, and `--all-*` removes one, which also admits all. Refuses a group declared in another file. |
| `rules promote <check>` | Copies a `converted` store check without a pending revision into the `check` of every rule file whose wording it covers, then deletes the check and those rules' entries from the store. Refuses when a covered rule is written in a step's `rules` list or already has a check. |

Every write loads the configuration once the files are written; when it does
not load, or the write would not have its effect, every file is restored and
the command fails with the reason. `--dry-run` validates the same way and
restores every file. Writes print the steps the rule or group reaches and
never commit. `rules --json` gives each rule a `store_check`: the approved
store check that runs for a rule without a command of its own.

`ww-rules.yaml`, next to `ww-agentic-workflows.yaml`, holds only a `rules`
mapping written by `rules add --group` and `rules filter`; ww rewrites it
whole and it is composed like any import. While it exists without being
imported, the write commands refuse to use it.

At a `check_disputed` stop the operator answers with `next`:

| Option | Effect |
| --- | --- |
| `--retry` | The check stands: the dispute is cleared, the rejection keeps counting toward `max_fixes`, and the next `next` hands the step back to its worker. |
| `--force --force-reason "<why>"` | Waives the disputed ID for this step: its check does not run, or its rule is not verified, when the worker completes again, and the artifact records the waiver. |

At the `fix_limit` stop `--force` waives every check and rule of the step. The
step record keeps its waivers as `checks_waived`, a mapping of ID to reason.
`next --yes` confirms `--retry`, `--force`, or `--approve` without the y/N
prompt, for an agent carrying out the operator's stated decision; the effect
or the approved command is still printed, and the audit record notes the
confirmation. `--yes` without one of them is an error. ww asks only at a
terminal: without one and without `--yes` it refuses at once, never reading
an answer from a pipe.

Every dispute is also appended to `.ww/rule-disputes.json`, beside the task
states, so `lint` can list disputed IDs without reading every task:

```json
{
  "schema_version": 1,
  "disputes": [
    {
      "check": "docs/header",
      "text_hash": "348d…",
      "task_id": "TASK-19",
      "run_id": "01-task",
      "step": "develop",
      "reason": "notes.md is a scratch file.",
      "attempt": 1,
      "disputed_at": "2026-09-30T10:00:00Z"
    }
  ]
}
```

`text_hash` is the disputed rule's wording hash, `null` for a hook or derived
check. An unknown key or `schema_version` is an error. The log is history: the
rule-automation store is not touched by a dispute.

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
