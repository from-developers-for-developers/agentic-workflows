# `../ww.yaml` specification

`../ww.yaml` defines what `ww` workflows do. The file is strict: unknown
keys, invalid types, and invalid references are errors.

## Common types

- **Name:** a non-empty string matching `[A-Za-z_][A-Za-z0-9_.-]*`.
- **Extension reference:** a qualified name such as
  `ext/ww/git/handlers:git-commit` where explicitly supported.
- **Description:** a string. It is optional unless stated otherwise.
- **Template:** `{{variable}}`, double braces everywhere. A name without
  `ww.` is always a variable an earlier step handed back (`variables`) or an
  automatic action returned. Every value ww provides lives under `ww.`:
  `{{ww.task.id}}`, `{{ww.task.workspace_dir}}`, `{{ww.task.workflows}}`
  (the configured workflow names), `{{ww.project.name}}`,
  `{{ww.project.dir}}`, `{{ww.project.names}}`, `{{ww.executable}}` (how
  printed commands invoke ww: `./ww` or the configured `executable`),
  `{{ww.documents.<name>}}`,
  `{{ww.metadata.<path>}}`, `{{ww.project_metadata.<path>}}`, on a per-item
  stage `{{ww.item.id}}`, `{{ww.item.text}}`, and `{{ww.item.field.<name>}}`
  for the stage's own item, and on a per-child stage `{{ww.child.*}}`. A
  configured extension may add values under `{{ww.<namespace>.<name>}}`:
  with `ww/git` listed in the settings, `{{ww.git.branch}}` (the task's
  branch), `{{ww.git.base_branch}}` (the branch it was created from) and
  `{{ww.git.branch_strategy}}` (the branch format key in use). Any other
  `ww.` name is an error at load, and a value its extension cannot give yet,
  such as the branch before ww/git recorded one, stops the task before an
  agent step reading it starts (`operator_reason: value_unavailable`;
  `next --retry` checks again) and fails an automatic handler reading it.

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
lives in `ww.json` (see the features guide), and a
`task_format` key in a YAML file is an unknown key.

## Imports

A root file — `../ww.yaml` or the root file of another
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
  then read exactly as a single `ww.yaml`; nothing is cached on disk.
  Every rule in this specification applies to that composed document.

## Configuration levels

Workflows come from up to three levels, applied top to bottom:

| Level | Root file | Required |
| --- | --- | --- |
| user | `ww.yaml` in `$WW_USER_CONFIG_DIR`, else `$XDG_CONFIG_HOME/ww/`, else `~/.config/ww/` | no |
| repo | `../ww.yaml` | yes |
| local | `../ww.local.yaml`, next to the repo file | no |

The repo file is what makes a directory a ww project; a user file alone
never does. A level is its root file plus the files that root imports, and
each level may use `imports` as described above. `init` creates the user
directory when it is missing.

- Levels fold in order, user first, each level's imports before its root
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
# ~/.config/ww/ww.yaml
handlers:
  - name: test
    argv: [pytest]
```

```yaml
# ww.local.yaml
modes:
  - economy: Keep answers short.
```

`ww.json` has matching user (`ww.json` in
the user directory) and `.local.json` levels, which are always deep-merged and take no `extends` key; `task_format`
is one of its keys, so a lower JSON level replaces it. See the features guide.

Projects, the directories a task may work in, are configured in
`ww.json` rather than here because their locations differ per
machine; see the features guide.

## Setup fragments

`ww setup apply <file> --for me|team` places configuration a setup skill
proposes; see the features guide for the command. The file is a YAML
fragment with any of the root keys `workflows`, `modes`, `profiles`,
`documents`, `handlers`, `hooks`, and `rules`, in this notation, plus an
optional `settings` mapping of `ww.json` keys:

```yaml
workflows:
  - review: Review a change before it is merged.
    steps:
      - read: Read the change and report what to fix.
modes:
  - gently: Suggest rather than insist.
settings:
  runtime: auto
```

Any other key, `imports` and `extends` included, is an error, and every entry
of a named catalog needs a name. The YAML part goes into a file ww owns and
rewrites whole, which the level's root file imports:

| `--for` | YAML part | Imported by | `settings` merge into |
| --- | --- | --- | --- |
| `team` | `ww-setup.yaml` | `ww.yaml` | `ww.json` |
| `me` | `ww-setup.local.yaml` | `ww.local.yaml`, created with only `imports` when missing | `ww.local.json` |

Each lives next to the repo file. An existing setup file keeps what the
fragment does not name: a workflow, mode, document, or handler of the same
name is replaced, a profile or rule group of the same name is replaced whole,
and hooks are appended phase by phase, skipping an entry identical to one
already in that phase. `settings` merge key by key, objects
recursively; a key that already holds a different value is a conflict, and
the whole apply is refused listing every one. A setup file the root file does
not import is refused, since ww would not know why it is not applied. A
fragment definition that the importing root file also defines is reported as a
warning: the root file folds after its imports, so it keeps its own.

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
| `handoff` | — | — | Removed; rejected with a message. A workflow transition (`handoff_to` on the last step) makes a handoff workflow; see the step key `handoff_to`. |
| `runtime` | `single` or `auto` | no | The runtime `start` uses for this workflow when `--runtime` is omitted; it outranks the project default in `ww.json`, and the flag outranks it. |
| `hooks_from` | string | no | The workflow whose global hooks this one runs with: a global hook filtered with `workflows` applies here when its filter admits this workflow's own name or the named workflow, so this workflow takes that lane's branch, worktree and commit handling. Extensions receive the lane as `ExtensionContext.lane` and key per-workflow settings by it: `ww/git` takes the lane's `branch_name_formats` and `base_branches` entries, while its records and `{{ww.task.workflow}}` keep the workflow's own name and `{{ww.task.lane}}` gives its formats and argv base-branch commands the lane. Rule groups and modes filtered with `workflows` keep matching the workflow's own name only: they are the lane's conventions, not its handling. Must name another workflow that has no `hooks_from` of its own. Set it in the workflow definition in `ww.yaml`. The plan freezes it, so a run keeps its lane. |
| `needs_hooks_from` | boolean | no | The workflow refuses to run until `hooks_from` is set: `start` (before a bootstrap request is opened), a handoff to it, and a replan of a run whose recompiled workflow lacks it (a `plan_changed` refusal). The message names `hooks_from` in the workflow definition in `ww.yaml`. Defaults to `false`. |
| `restartable` | boolean | no | A new `start` of this workflow while its previous run is unfinished abandons that run and opens a new one; the abandoned run stays in the task's history. Without it, a task with an unfinished run refuses another start. An unfinished run of a different workflow is never abandoned this way. Defaults to `false`. |
| `inherit` | workflow name | no | Copy that workflow completely: steps, workflow hooks, and every setting. The workflow's own keys other than `steps` and `hooks`, which it may not declare, replace the copied values. A global hook filtered to the inherited workflow also runs for this one. Chains are allowed; a cycle or unknown name is an error. |
| `recommended_next_workflow` | workflow name or null | no | Offered to the operator when a run completes: the page asks through the agent's choice menu and shows the `start` command for the same task, to run only on confirmation. Inherited like any setting; `null` clears an inherited one. Invalid in a handoff workflow (one with a `handoff_to` transition). |

Execution settings inherit from workflow to enclosing steps to the current
step. Hooks then apply the referenced root handler and the invocation override.
Omission inherits; explicit `model: auto` or `reasoning: auto` stops inheritance.
Model and reasoning are coupled: when a child changes the effective model and
omits reasoning, reasoning resets to `auto`. Repeating the same model preserves
the inherited reasoning. `agent` falls back to the agent passed to `plan` or
`start` and may not be `auto`.

`../ww.json` sets ww-wide project behavior separately from workflow
syntax. `enabled` is `true` (the default: agents use ww for project work),
`false` (agents do not use ww, `discover` says only that, and `start` refuses),
or `"on_request"` (ww stays available, but agents use it only when the user
explicitly asks for it; `discover` says so before its full catalog and reports
`"enabled": "on_request"` in JSON); any other value is an error naming the three.
`limits` holds two positive integers, each defaulting to `3`: `rounds`, the
round limit of a step `loop` without its own `max_rounds`, and `fixes`, the
rejected completions a check allows when its rule sets no `max_fixes`; any
other key in it is an error. `agent_hooks` holds `check_unfinished`, a
boolean defaulting to `true` that decides whether the `session-start` hook
lists unfinished tasks, and `recent_days`, a positive integer defaulting to
`3`: the window of that hook's scan, of `ww interrupted`, and of the
interruption pointer in `discover` and `lookup`; any other key in it is an
error. The file may
also override the internal requests of the implicit init action, `cheapest` /
`low`, and of the workflow-summary action, `auto` / `auto`. `workflows` switches
off [built-in workflows](#built-in-workflows) by name, such as `catchall`; each
entry is an object whose only key, `enabled`, defaults to `true`, and a name ww
does not ship is an error listing the built-in ones. A workflow of the same
name in any `ww.yaml` level replaces the built-in one instead.
`executable` names the ww binary the project runs, a command on `PATH` or a
path; every command ww prints starts with it, and the `./ww` launcher runs it.
Without it, printed commands use `./ww` and the launcher runs
`ww-agentic-workflows`; `init` writes `ww-agentic-workflows` when it is
missing. The launcher is ww-owned: `init` rewrites a `./ww` that differs from
the current template, and `lint` warns about one. `runtime` (`single` or `auto`) is the runtime `start` uses when
neither `--runtime` nor the workflow names one, `update_check: false` silences
the notice that the ww checkout is behind its remote, `task_format` is the
generated task ID format, `rules` holds [the guidance for building
checks](#guiding-checks-rulescheck_guidance), `projects` lists the
directories a task may work in, and `extensions` holds each extension's
settings. Missing fields retain their individual defaults; this is every key
with its default, as `init` writes it:

```json
{
  "enabled": true,
  "runtime": "single",
  "update_check": true,
  "feedback_learning": true,
  "executable": "ww-agentic-workflows",
  "task_format": "TASK-{{uuid}}",
  "limits": {"rounds": 3, "fixes": 3},
  "agent_hooks": {"check_unfinished": true, "recent_days": 3},
  "rules": {},
  "builtins": {
    "init": {"model": "cheapest", "reasoning": "low"},
    "workflow_summary": {"model": "auto", "reasoning": "auto"}
  },
  "workflows": {},
  "projects": [],
  "extensions": {}
}
```

### Built-in workflows

ww ships workflows of its own as YAML files inside the package
(`ww/assets/workflows/*.yaml`), written in this notation. `catchall`, which
records a change no configured workflow covers, is one of them; ww's learning
and setup workflows are others. Together they form a built-in level below the
user level:

- A workflow, document, or mode that any configuration level defines under the
  same name replaces the built-in one. A project that declares a document of a
  built-in's name keeps its own, and the built-in workflow uses it.
- A built-in file may declare, besides `workflows`, the root `documents` and
  `modes` that belong to them, and nothing else. They come along while any of
  the file's workflows is enabled; when `workflows` in
  `ww.json` switches all of them off, the file contributes
  nothing.
- Built-in workflows follow the configured ones. They are added after the
  levels are composed, so `extends: false` never removes them.
- `discover` lists `catchall` under its own heading, and the other
  built-in workflows under "ww's own workflows" (`builtin_workflows` in JSON),
  apart from the project's.
- A built-in's `recommended_next_workflow` naming a workflow that is switched
  off is dropped.

The built-in workflows:

| Workflow | File | Purpose |
| --- | --- | --- |
| `catchall` | `catchall.yaml` | Records a change no configured workflow covers. |
| `ww-learn` | `onboarding.yaml` | Interviews the operator into the documents `me`, `myrole`, `team` and `company`. |
| `ww-express` | `onboarding.yaml` | Infers the documents `me`, `myrole`, `team` and `company` from the repository and writes them once the operator confirms. |
| `ww-learn-project` | `onboarding.yaml` | Scans how the project's work is organised into `project`. |
| `ww-suggest` | `onboarding.yaml` | Designs a setup with the operator, proposes it in full, and places it with `setup apply`. |
| `ww-solve` | `onboarding.yaml` | Proposes a change for a problem the operator describes. |
| `ww-rules-from-artifacts` | `onboarding.yaml` | Proposes rules from past artifacts of chosen steps. |
| `ww-automate` | `onboarding.yaml` | Proposes a script and its handler for a step's mechanical work. |
| `ww-scriptize-rules` | `scriptize.yaml` | Scriptizes every rule with no check yet into checks, built and proven on a branch of its own from the required `ww/git` default base, following its worktree settings; automatic Git hooks, `restartable`. |

`onboarding.yaml` also declares the documents `me` (`scope: user`), `myrole`,
`team`, `company` and `project` (`scope: project`, `path: .ww/<name>.md`;
`myrole.md` stays git-ignored, personal to the checkout),
`setup_proposal` (a task document at `.ww/tasks/{{ww.task.id}}/setup-proposal.yaml`),
and the mode `ww-narrate`. See the features guide,
[Setting ww up](features.md#setting-ww-up-learning-and-suggestions).

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
by all tasks; `scope: user` puts it in the user configuration directory (see
[Configuration levels](#configuration-levels)) as `<name>.md`, shared by every
project of the user, and ww creates that directory when it resolves the path:

```yaml
documents:
  - test_cases: The test cases derived from the issue, kept current across runs.
  - conventions: Conventions every task follows.
    scope: project
  - issue_notes: Notes kept with the repository, on the task's branch.
    path: documentation/issues/{{ww.task.id}}/notes.md
  - me: Who the operator is and how they like to work.
    scope: user
```

`path` places a document outside `.ww`. It is relative to the project root and
must stay inside it; a task-scoped path may use `{{ww.task.id}}`. When a run has a
working directory, such as a Git worktree, a task-scoped `path` resolves inside
that directory, so a document kept in the repository lands on the task's
branch; a project-scoped `path` always resolves against the project root. A
user-scoped `path` is relative to the user configuration directory and must stay
inside it; neither a project nor a user path may use `{{ww.task.id}}`. The
update journal stays under `.ww` in every case: for a user document it records
what this project's runs did to the shared file.

A step or handler that maintains a document lists it in `saves` as
`documents.<name>`, with the text instructing the update. Its worker may create
or edit that file in place, the one exception to the rule against writing
under `.ww`, and the file must exist when the step completes.
`{{ww.documents.<name>}}` in any
description resolves to the file's absolute path on the filesystem ww runs in,
whether or not the file exists yet:

```yaml
- derive_tests: Analyze the issue and derive test cases.
  saves:
    - documents.test_cases: One checklist item per case; keep items that still hold, mark superseded ones.
- report: Build the test report from {{ww.documents.test_cases}}.
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
scope and does not accept a `steps` filter. Use `before_start` for
preparation local to `init` or another step. A top-level declared step may use
`artifact_from: init` to
receive the requirements artifact. Handoff targets and child-task workflow runs
each begin with their own `init` step.

A step accepts every [handler key](#handlers), plus:

| Key | Type | Meaning |
| --- | --- | --- |
| `hooks` | hooks mapping | Hooks local to this step. |
| `rules` | list of rule entries | The step's own rules and the rule groups it names; see [Step rules](#step-rules). Not allowed on a step without agent work of its own, such as a container or a command. |
| `profile` | profile value | Overrides the profile inherited from the workflow and every enclosing step. Nested steps, loop bodies, and per-item stages inherit it in turn. |
| `role` | `manager` or `worker` | Who performs the step. `manager` keeps it in the managing session in every runtime and ignores its profile, agent, model, and reasoning settings; `worker`, the default, lets an `auto` run delegate it. In `auto` the manager completes a `manager` step with `complete --role manager` (or `loop --role manager`), and `complete` or `loop` with `--role worker` on it is refused. Inherited from the workflow and every enclosing step, like `profile`; nested steps, loop bodies, and per-item stages inherit it in turn. Only agent steps take it: on a step ww runs, such as a command, it is an error. |
| `subagents` | boolean | When `false`, whoever performs the step, the manager or a worker, does all of its work alone and spawns no subagent for anything; the step's page says so. It says nothing about who performs the step (`role`) or with which model. Inherited like `profile`; a nested step may set `true` again. Defaults to `true`. |
| `explicit` | boolean | Requires the agent to describe each meaningful operation before doing it and show concrete edits or a focused diff after each changed file. Large changes may use a concrete diff artifact; changed files must remain individually named and secrets redacted. Inherited from the workflow and structural groups, loops, item stages, and child stages; a child may set `false` to opt out. Defaults to `false`. |
| `interactive` | `true`, `false`, or `page` | `true`: the step is a normal conversation with the operator, held by the session that can talk to them; it implies `role: manager`, and `role: worker` beside it is an error. The agent responds to questions and corrections and treats clear contextual completion as permission to finish, asking naturally if the intent is ambiguous. `Done for today` may mean pause and resume later. Completion is refused until the conversation was recorded with `interact` and ended; an open interaction cannot be completed. `page`: the operator answers this stage on the operator page, an answer sheet over every item that `interact --await` serves while the agent waits and applies when the wait ends; valid on one per-item stage per `items` step. Defaults to `false`. |
| `learnable` | boolean | Opts this step's completed artifact into optional feedback deduction after workflow completion. Independent of `interactive`; defaults to `false` and requires `artifact: true`. |
| `choices` | list of choices | Options the operator picks from during an interactive step, `- <label>: <description>`; the label is shown as written. The agent follows the host's actual question-tool schema, using structured options when offered and a text-only question only when required, and otherwise presents a numbered list in chat. An asynchronous answer remains pending until the operator explicitly answers; timeout, dismissal, or preselection is not an answer. The pick must be recorded before the interaction ends. Requires `interactive: true`. |
| `steps` | list of steps | Nested ordered steps. |
| `loop` | non-empty list of steps | Repeats ordinary nested steps until an authorized worker stops it. |
| `max_rounds` | positive integer | Overrides the project-wide maximum number of rounds for this loop. Valid only beside `loop`. |
| `assignment` | `per_round` or `per_step` | How the body steps are split into worker assignments in the `auto` runtime; default `per_round`. Valid only beside `loop` on a step; `items` and `children` take their own `assignment` inside their mapping. Any other value is an error listing these. |
| `break` | non-empty string | On an agent-owned loop-body step, grants permission to break its enclosing loop when this condition holds. |
| `continue` | non-empty string | On an agent-owned loop-body step, grants permission to continue from the beginning of its enclosing loop when this condition holds. |
| `artifact` | boolean | Whether agent completion requires an artifact; default `true`. |
| `artifact_from` | name | Earlier artifact-producing step whose artifact is supplied to this step: an earlier sibling, or an earlier step of an enclosing level, the nearest one first. Inside assessment outcomes the assessment itself is eligible, and inside per-item stages the `items` step, each supplying its own artifact; an enclosing loop or group is not. A plain group, or an assessment named after its outcomes, supplies the artifact of the latest step inside it (inside the chosen outcome) that saved one in its current round (inside a loop, the loop's current iteration only), and needs some step inside that can save one; when none did, an assessment supplies its own artifact if it saved one, and otherwise the step is told that no artifact is available. An assessment whose outcomes cannot save an artifact supplies its own. |
| `items` | `null`, string, or mapping | Collects work items, then runs per-item stages for each; see [Items](#items). |
| `handoff_to` | workflow name or `{{variable}}` | Makes the workflow a handoff workflow and ends it by starting that workflow as the task's next run. A workflow has at most one transition, and nothing may follow it: valid only on the last top-level step (with no completion hook applying to it), with `description`, `agent`, `model`, and `reasoning` at most; or on a hook, see [Hooks](#hooks). `workflow` only runs a child, under `children`. |
| `item_phase` | `analyze`, `resolve`, or `report` | On a per-item stage: the standard item fields the stage fills, the analysis (`processed_item`), the solution and `resolved`, or `reported`. |
| `children` | mapping | Collects child tasks with the step's own action, then runs every child with one workflow, or runs the parent's own stages once per child; see [Children](#children). |
| `handler` | handler name | Copies a root handler definition into this step; the step keeps its own name and any explicit step fields override the copied values. A step with no content of its own, `- fetch_requirements: ~`, and a root handler of the same name copies that handler implicitly. |

`steps`, `loop`, `items`, and `children` are alternatives. A loop wrapper cannot
also declare an action or collection; its `loop` entries are ordinary steps and
may use hooks, profiles, item collection, nested steps, and the other step
features. A pure `steps` group or loop wrapper cannot be `interactive: true`
because it does not execute its own conversation; make an executed child step
interactive instead. An item or child collector remains a real step and may be
interactive. `item_phase` is invalid together with `items`.

The manager enters a loop and dispatches its body. With the default
`assignment: per_round`, one worker carries consecutive body steps of
one round while they resolve to the same agent, model, reasoning, and profile;
a body step that overrides any of them starts a new assignment, because a
running worker cannot change them. `per_step` hands every body step back to
the manager. Every body step's instruction states which round of the loop it
belongs to, so a later round concentrates on the previous rounds' work rather
than on the whole task. One or more agent-owned steps may declare `break` or
`continue`. After doing such a step, its
worker evaluates that step's break gate. If it passes, the worker runs the
displayed `ww loop <TASK-ID> --break --role worker` command (`--role manager`
on the manager's own step under `auto`) instead of `complete`;
that command records the step result and deterministically exits the enclosing
loop after the step's completion hooks. The break result is also saved as the
loop wrapper's main artifact, outside its round directories. A wrapper may
set `artifact: false` to disable only this main artifact; body steps retain
their own artifact settings.

If a worker uses `continue`, ww records the result, runs the step's completion
hooks, then resets the body and dispatches its first step. If no worker breaks
or continues the loop, reaching the end resets the body and the manager
dispatches the first step again until the effective maximum is reached. The
effective value comes from the wrapper's `max_rounds`, or from `limits.rounds`
in `../ww.json` when the wrapper omits it, and is frozen in the saved
workflow plan. At the limit, ww does not expose a continuation command: it
reports `awaiting_operator` with `operator_reason: loop_limit` and a warning
that must be escalated to the user for manual resolution, and shows the operator's exit, `next --force --reason`,
which leaves the loop and continues with the steps after it.

`max_rounds` is invalid without `loop`; use `break` for a worker-controlled
loop exit.

```yaml
- code-review-in-a-loop: ~
  max_rounds: 5
  loop:
    - code-review: Review the development or the previous round of fixes.
      break: There are no meaningful code-review findings.
    - fix: Record each code-review finding as an item.
      items: ~
```

### Learnable artifact sources

An artifact-producing step accepts `learnable: true` or `false` (default
`false`). This is independent of `interactive`; it opts the step's completed
artifact into post-workflow negative-feedback deduction. `learnable: true`
requires `artifact: true`. No deduction step or completion gate is compiled
into the plan. The setting is inherited when referencing a reusable step and
can be explicitly overridden. Saved plans retain it so completed-source
selection does not depend on later workflow edits.

### Children

A `children` step splits the task into child tasks and runs them. Its own
action collects them: the agent records each child with `add-child`, and the
step cannot complete without one. ww then runs every child with
`children.workflow`, one at a time, and the parent continues after the last
child completes.

```yaml
- split-work: Split the feature into stories.
  children:
    description: One child per story.   # optional splitting guidance
    workflow: implementation
```

| Key | Value | Meaning |
| --- | --- | --- |
| `workflow` | workflow name | The workflow every child runs. Required unless `steps` is given; the two are exclusive. |
| `steps` | list of steps | The parent's own stages, run once per child; see [Per-child stages](#per-child-stages). |
| `assignment` | `per_step` | How the per-child stages split into worker assignments; only with `steps`, and `per_step` is the only value built. `per_child` is reserved and rejected until it is built. |
| `description` | non-empty string | Splitting guidance shown to the collecting agent, as `items.description`. |

The run is compiled as a ww-owned item nested under the collecting step,
`<step>/children`, so the step's completion hooks run after every child has
finished. A workflow may contain at most one `children` step, and a children
step cannot sit inside per-item stages; the named workflow must exist and
cannot itself use `children` (child tasks are one level deep). `children`
cannot be combined with the step's own `handoff_to` transition.

`add-child <task> [--id ID] --text TEXT [--project NAME] [--field
NAME=VALUE]...` records a child during the collecting step; `--field` gives it
custom fields, as `add-item --field` does for items.
`update-child <task> <child> [--text TEXT] [--project NAME] [--field
NAME=VALUE]...` changes a child's text or project while it has not started yet
(status `pending`): during the collecting step, in a per-child stage before the
child runs, and while the parent waits for its children; a started child is
refused with its status. Its custom fields only feed the parent's per-child
stages, so `--field` may change them at any time.

`start-child <parent> <child> [--workflow NAME] [--runtime single|auto]
[--model MODEL] [--reasoning LEVEL]` can start the child in a different session configuration
from its parent. Omitted options inherit the parent's settings; changing the
model without specifying reasoning resets reasoning to `auto`, while repeating
the inherited model preserves its reasoning. These options do not change the
parent or the child workflow's configured step settings. `--workflow` selects
a different child workflow instead of the coordinator's configured target.
The selected workflow must exist and cannot contain `children`; temporary
identity requests require a first-step `task_id` variable in that target.
The selection is frozen before launch and reused on retries without repeating
the flag. A starting or started child cannot change workflows. Under `single`, the
chosen session performs all child assignments without subagents; its actual
host model and reasoning settings remain authoritative, so launch that session
with the requested settings. ww records guidance rather than switching models.

The resolved launch settings are saved before starting the child, including
when it first obtains an external ID. A retry can omit the flags and keeps the
saved settings. Conflicting overrides after launch begins are refused, and
already-started children cannot be reconfigured through `start-child`.

#### Per-child stages

With `steps`, the parent owns a loop over its children: once collection
completes, ww runs the stages for the first child, then for the next, strictly
one child at a time. Exactly one top-level stage carries `workflow:`; inside
`children` it does not hand off, it starts the current child task with that
workflow and waits for it to finish.

```yaml
- slices: One child per slice of the plan, with the slice's full text.
  children:
    steps:
      - refine: Adjust {{ww.child.text}} to what earlier slices actually landed.
        role: manager
      - implement:
          workflow: task
      - review: Review {{ww.child.git.branch}} against the slice.
        artifact_from: implement
        role: manager
      - land: Merge {{ww.child.git.branch}} into {{ww.git.branch}}.
```

- The stages run in the parent task and run, with the parent's hooks,
  profiles, rules, and roles; they are compiled like per-item stages, as
  templates under `<step>/{child}` that become `<step>/child-1/...`,
  `<step>/child-2/...` when collection completes.
- A stage reads its child as `{{ww.child.id}}`, `{{ww.child.text}}`,
  `{{ww.child.project}}` (empty in the root), `{{ww.child.field.<name>}}`, and
  every extension value of the child's own task as `{{ww.child.<namespace>.<name>}}`,
  for example `{{ww.child.git.branch}}` and `{{ww.child.git.base_branch}}`. The
  exact names are checked when the plan is compiled, and only a per-child stage
  may read them. A stage before the `workflow:` stage runs before the child
  task exists, so it may read only `id`, `text`, `project`, and `field.*`; a
  child extension value there is rejected at compile time. A value that is not
  available yet, such as the branch of a child whose task has recorded none, or
  a field the child does not carry, stops the task before the stage starts
  (`operator_reason: value_unavailable`), as for `{{ww.git.*}}`; for a missing
  field the error names the `update-child ... --field` command that sets it.
- The `workflow:` stage takes a name and an optional `description`, and may be
  written `- implement: {workflow: task}`. It shows the manager the
  `start-child` command for its own child; any other child is refused while it
  waits. Its artifact is the child's workflow summary, so later stages use it
  with `artifact_from: implement`.
- Stages before it may refine the child with `update-child`; the child's `init`
  records the text it has when it starts as its requirements.
- In `auto`, the parent's manager also manages the child: starting it returns
  the child's page, and the child's steps are ordinary worker assignments the
  same manager dispatches. A completed child's page names the parent command
  to continue with. The stages are assigned one per step
  (`children.assignment: per_step`).
- A child that fails stops the parent (`awaiting_operator`, `child_failed`), as
  in the simple form.
- `break` on a stage ends the loop over the children: the stage's completion
  hooks run, every remaining per-child stage is skipped, and each child that has
  not started is marked `skipped`; the parent continues after the `children`
  step. A `break` inside a `loop` within a stage ends that loop only.
  `continue` needs a loop of its own inside the stage.
- Validation: exactly one top-level `workflow:` stage; no `handoff_to`
  transition anywhere inside `children.steps` (stages, nested steps, or
  hooks); the
  child workflow may not use `children`; stages may not use `items` or
  `children`; `steps` and `workflow` directly under `children` are exclusive;
  a step with `children.steps` may not sit inside a `loop` (the stages expand
  once, when collection completes), while the simple form may.

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
          break: There are no meaningful review remarks.
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
Such an entry, as a step or as a hook, may carry `workdir` and `args` and no
other key; everything else about the handler is the extension's to define.
`args` is the handler's positional arguments, templates allowed, and must
match the number of arguments it declares:

```yaml
- name: ext/ww/git/handlers:merge-branch
  args: ["{{ww.child.git.branch}}", "Land slice {{ww.child.id}}"]
```

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
| `analyze`, `resolve`, `report` | non-empty string | Guidance for the analyze, resolve, or report phase of the built-in `handle-item` stage. Invalid together with `steps`. |
| `variables` | list of variables | Values the built-in `handle-item` stage hands back on each item's completion. Invalid together with `steps`. |
| `saves` | list of saved values | Metadata, documents, or item fields the built-in `handle-item` stage saves on each item's completion. Invalid together with `steps`. |
| `interactive` | `true` or `page` | `true` makes the built-in `handle-item` stage a conversation with the operator, for example a manual test the operator performs and reports; `page` has the operator answer it on the operator page, and `interact --await` completes it from the answer. Invalid together with `steps`. |
| `choices` | list of choices | Options the operator picks from in the built-in `handle-item` stage. Invalid together with `steps`. |
| `steps` | list of steps | The per-item stages. Omitted, one built-in `handle-item` stage runs per item. `[]` collects items without processing them. |
| `assignment` | `together`, `per_item`, or `per_step` | How per-item stages are split into worker assignments in the `auto` runtime; default `together`. |
| `persistent` | boolean | The items outlive the run: every run of the task starts from the task's stored items with their outcomes cleared, and the collection step reconciles that list against the source instead of splitting again. Defaults to `false`. |
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
    assignment: per_item
    model: sonnet
    reasoning: low
    steps:
      - analyze: Analyze this comment.
        item_phase: analyze
      - fix: Resolve this comment.
        item_phase: resolve
      - reply: Report the outcome.
        item_phase: report
```

Worker settings cascade from the `items` step, to `items`, to each stage, and
the most specific value wins. The step's own `agent`, `model`, `reasoning`,
`profile`, `role`, and `subagents` apply to collection and are inherited by the stages;
the same keys under `items` apply only to the stages. The cascade reaches each
configured stage directly; stages nested deeper inherit like ordinary nested
steps.

`assignment` changes only where worker assignments end in the `auto`
runtime:

- `together`, the default, gives one worker every stage of every item,
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

The step's `before_start` hooks run before collection, and its completion
hooks run after the last item. Each stage keeps its own hooks. Collection does
not require an artifact, because its result is the recorded items.

A workflow may contain at most one `items` step, at any nesting level. This is
an intentional limitation: collected items belong to the workflow run, and ww
expands every per-item stage in one place when collection completes. An `items`
step cannot also declare `steps`, `loop`, `item_phase`, or child
tasks.

## Handlers

Each root handler, and each step through the same shared shape, accepts:

| Key | Type | Required | Meaning |
| --- | --- | --- | --- |
| `name` | name | yes* | Identifier; key omitted in shorthand or for an inline hook command. |
| `description` | string | no | Instruction or explanation. |
| `kind` | `skill`, `slash_command`, or `prompt` | no | `skill` requires a discovered agent skill with this name, `slash_command` a discovered slash command, and `prompt` selects plain agent work instead of skill or slash-command resolution. |
| `mcp` | non-empty string | no | MCP connection; `description` supplies its work instruction. |
| `handlers` | non-empty list of handler mappings | no | Ordered, fully automated actions. Members may reference catalog handlers, use inline commands, or nest automated groups. Agent-owned work and actions requiring agent input are rejected. Exclusive with a direct action or step container. |
| `argv` | string list | no | Automatic argument-vector action run by `ww`. |
| `shell` | string | no | Automatic shell action run by `ww`. |
| `args` | string list | no | Positional arguments for `shell`, or, beside only `name` and `workdir`, for the extension handler that `name` references, which must declare exactly that many. Templates are allowed. |
| `env` | string mapping | no | Environment values for `shell`. |
| `assert` | list of conditions | no | Conditions the command output must all meet; see [Commands](#commands). |
| `on_failure` | `fix` or `operator` | no | Automatic shell/argv handlers only. `fix` opens an agent repair assignment after a known failure; completing the repair asks ww to retry the handler. Default `operator` stops for an operator decision. |
| `on_failure_instruction` | non-empty string | no | Optional guidance for shell/argv handlers, included in the repair assignment or a hook failure page. Templates are allowed. |
| `idempotent` | boolean | no | Running the command action again is harmless: an interrupted run is replayed by `next` instead of waiting for an operator. Requires `argv` or `shell`. Defaults to `false`. |
| `action` | mapping | no | The registry form, `{type: <action>, ...}`: selects a registered action by its identifier, with that action's own keys beside `type`. Extensions' actions use it; it cannot be combined with `kind`, `mcp`, `argv`, `shell`, `args`, `env`, `assert`, or `idempotent`, and the core controls (`loop`, `workflow_transition`, `child_workflow`) are refused as types. |
| `variables` | list of variables | no | What the step hands back, read later as `{{name}}`; see [Variables](#variables). |
| `saves` | list of saved values | no | What an action writes to metadata, documents, or its item; see [Saves](#saves). |
| `agent` | non-empty string other than `auto` | no | Preferred executor guidance. |
| `model` | non-empty string | no | Model guidance; `auto` stops inheritance. |
| `reasoning` | non-empty string | no | Reasoning guidance for this action. |
| `workdir` | `task`, `project`, or `root` | no | The directory this action works in; see [Working directory](#working-directory). Defaults to `task`. |

### Automated handler groups

Use `handlers` to declare a reusable sequence that ww executes itself:

```yaml
handlers:
  - build-frontend: ~
    shell: npm run build
  - build-all: ~
    handlers:
      - build-frontend: ~
      - argv: [test, -d, dist]

workflows:
  - name: task
    steps:
      - build-all: ~
```

Unlike `steps`, which can include agent work, this list accepts only automatic
actions requiring no agent-supplied inputs. Named references resolve against
the handler catalog, including later declarations; recursive reference cycles
are errors. Groups can nest. `handlers` cannot be combined with a command,
agent action, `steps`, `loop`, `items`, or `children` on the same definition.
Use `steps` for sequences that contain manual work or step-specific lifecycles.

Members run in declaration order under the enclosing step or hook's identity.
They do not create nested workflow steps or agent completion assignments.
The group may provide defaults for `workdir`, worker guidance used for repairs,
`on_failure`, and `on_failure_instruction`; each member or referenced definition
can override them. On failure the normal handler policy applies, and a retry
preserves successful earlier members.

A named automated group can also be referenced by a hook. Its members keep
that hook phase and filters; a `before_complete` hook with `on_failure: fix`
compiles eligible members into individual completion checks. Existing hook
`handlers` groups continue to accept their existing action types, including
agent actions: the automatic-only requirement belongs to reusable handler
definitions and inline workflow handler groups.

### Automatic handler repairs

An automatic command step runs when ww reaches it, records its own success,
and advances without an agent completion for that step. With `on_failure: fix`,
a known failure pauses that execution and opens a repair assignment:

```yaml
- build-root-assets: ~
  shell: docker compose exec -T -w /var/www/html/frontend requesttool npm run build
  on_failure: fix
  on_failure_instruction: Fix the reported build errors.
```

The repair page includes the command, failure diagnostics, full output artifact
references, and the optional instruction. The agent fixes the cause and calls
the displayed `complete` command with a repair artifact. ww retries the failed
handler; only its success completes the automated step. Completed preceding
steps stay completed. Repair assignments are attached to the execution; they
create no extra workflow steps and invoke no step hooks of their own.

In the `single` runtime the same session receives the repair immediately. In
`auto`, ww ends the previous assignment and the manager dispatches the repair
with `next`; successive failures of the same handler stay with that repair
worker. The handler's agent/model/reasoning/profile guidance applies to its
repair assignment. Repair artifacts and command output survive reloads and are
available through `artifacts`. JSON repair pages include `handler_repair` with
the item ID, attempt count, limit, instruction, output references and artifacts.

The handler stops for the operator with `operator_reason: fix_limit` after
`limits.fixes` failures (default 3), using the same counting policy as checks.
An operator-authorized `next --retry` starts a fresh budget and retries the
handler; `next --force --reason "..."` skips it. An agent that cannot repair it
can use the displayed `fail` command to request an operator decision.

`on_failure: fix` authorizes retry after a known failure without requiring
`idempotent: true`. An interruption with an unknown outcome still follows the
usual automatic-handler recovery rules: only `idempotent: true` permits
implicit replay. These settings apply to automatic steps as well as commands
used in hooks. Existing `before_complete` checks return failures to their
step's worker rather than opening a separate repair assignment.

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
`{{ww.task.workspace_dir}}`, which resolves to that directory for the step.
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
      - update-shared-notes: Update the git-ignored notes in {{ww.task.workspace_dir}}.
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

An action uses one form: `kind` (`skill`, `slash_command`, or `prompt`),
`mcp`, `argv`, `shell`, or the registry form `action`. These forms cannot be
combined. `kind: prompt` explicitly selects plain agent work. With no explicit
action, `ww` resolves the name as a project skill,
slash command, or ordinary agent prompt, in that order. To run several
commands, use a hook's `handlers` list, one `argv` or `shell` each.

### Variables

`variables` lists what a step hands back, read by later steps as `{{name}}`.
An entry `- name: description` (or `name` with an optional `description`) is a
value the performer supplies with `complete --variable name=<value>`; on an
automatic action it is input the agent supplies before ww runs the command. A
bare string, `- name`, is a value an automatic action returns itself, such as
an extension handler's result:

```yaml
variables:
  - workflow: The workflow name corresponding to one of {{ww.task.workflows}}.
  - reason: ~
```

Names must be unique in the list and may not start with `ww` as their first
dot-separated segment, or with `__`: those are ww's own values. Dots are
allowed in a name.

`{{ww.choices}}` is scoped to the effective current step after reuse and
compilation. Its value is a JSON array of configured choice labels in order,
or `[]` when the step has no choices. It is read-only guidance for instructions
and provided-variable descriptions; it does not validate the value supplied.

Within one completion window, matching supplied-variable declarations (the
same name and description) share one input across handlers, in first-request
order. Conflicting declarations for a name are an error identifying that name
and both plan items. Repeating a supplied `--variable` remains an error.

### Assessments

`assess` asks the agent to choose a named outcome before work continues. Prefer
`positive` or `negative` when the evidence supports either; reserve `mixed` for
material uncertainty. Select the result with `next --outcome <label>`.

The standard branches may be written beside `question` as `positive`,
`negative`, and `mixed`, using the same ordinary step shapes as entries under
`outcomes`. Do not combine these direct branches with `outcomes`; use
`outcomes` when custom labels are needed. Omitted standard outcomes retain the
existing behavior of doing no branch work and continuing after the assessment.

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

### Saves

`saves` lists what an action writes: each entry is a prefixed path
and the text saying what to put there. The prefix is the kind and scope of the
value and the rest its storage path; no `ww.` prefix is written, since an
entry can only name something ww manages.

| Entry | Saves | Passed with |
| --- | --- | --- |
| `metadata.<path>` | task metadata at `<path>`, read as `{{ww.metadata.<path>}}` | `complete --metadata <path>=<value>` |
| `project_metadata.<path>` | project metadata shared by every task, read as `{{ww.project_metadata.<path>}}` | `complete --metadata project_metadata.<path>=<value>` |
| `documents.<name>` | a root document, created or edited in place; see [Documents](#documents) | the file itself |
| `item.field.<name>` | a custom field of the step's item; on the collection step, of every collected item. Completion is refused while any is empty. | `update-item --field <name>=<value>`, several per call |

A metadata entry may add `append: true`: the path holds a list, each
completion may pass it once per value or omit it, values are appended to what
is stored, and repeats are dropped. The shorthand form is the usual one:

```yaml
saves:
  - metadata.jira.issue_id: The issue key.
  - project_metadata.last_auto_refactored_at: Save the current timestamp in YYYY-MM-DD HH:mm format.
  - metadata.labels: Every label the issue carries.
    append: true
  - documents.test_cases: Keep one checklist item per case.
  - item.field.bitbucket_reply_id: The ID of the reply you posted.
```

Within one action, paths must be unique, and metadata paths in the same scope
cannot overlap (for example `metadata.jira` and `metadata.jira.issue_id`).
`project_metadata.ww` and every path under it are reserved for ww's own state,
such as `ww.setup.done`, and rejected.
Agent-owned actions supply metadata through `complete --metadata`. Shell and
argv handlers automatically save stdout when they declare metadata in `saves`:

```yaml
handlers:
  - create-github-pr: ~
    argv: [gh, pr, create, --fill]
    saves:
      - metadata.github.pr_url: The pull request URL.
```

The saved value is complete stdout with leading and trailing whitespace
removed; stderr is excluded. Multiline output is one value, and empty output
saves an empty string. Each metadata entry receives the same whole output;
`append: true` appends it as one list element with the usual deduplication.
Metadata is saved only after successful exit and all assertions pass. The
completion and its publication intents are committed before publication;
interrupted publication resumes without replaying the successful command.
`from` is not a supported save option. Documents and item fields remain
agent-owned.

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
  - equals: clean
```

`assert` is a non-empty list of conditions that must all hold: `empty`, which
requires the output to be empty or whitespace, and `{equals: <value>}`, a
non-empty string the whole output must equal:

```yaml
shell: grep -l TODO $WW_STEP_CHANGED_FILES || true
assert: [empty]
```

## Hooks

A hooks mapping (workflow hooks; the agent's own hooks are `ww hook`, see
agent-hooks.md) accepts these lifecycle phases, each containing a list:

- `before_start_workflow`
- `before_start`
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
| `on_failure_instruction` | non-empty string | Optional failure guidance, also accepted on each member of `handlers`; a member inherits the group instruction unless it overrides it. |
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
A hook can reference an automated `handlers` group, expanding its actions in
order under the same phase and filters. It cannot reference a root handler that defines
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
a workflow transition with `handoff_to` plus optional `description`, `agent`,
`model`, and `reasoning`. A transition hook belongs only on the
`after_complete` hooks of a workflow's last top-level step, as their last
entry, since nothing runs after a handoff; at global or workflow scope it is
rejected:

```yaml
steps:
  - choose: Choose the next workflow.
    variables:
      - next_workflow: The workflow to run next.
    hooks:
      after_complete:
        - handoff_to: "{{next_workflow}}"
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
  assert: [empty]
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
| `max_fixes` | positive integer | Rejections this check allows; defaults to `limits.fixes` in `ww.json` (3). |
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
        assert: [empty]
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
an assignment of its own, and asks for a verdict on each of its rules.

When the step begins, each rule without a command resolves once against the
rule-automation store, and the step keeps what it resolved: `converted` when
its wording has a `converted` check, which then checks it and asks no
verifier (subject to its `config` files; see [Rule commands](#rule-commands)),
else `judged`, whatever other status the store gives the wording. A verifier
never writes the store.

A verification item completes with its findings as `--artifact` and one
`--rule-result` per rule, a repeatable JSON object; `complete` refuses a
missing, unknown, duplicate, or malformed one, and the option on any other
step.

| `--rule-result` key | Meaning |
| --- | --- |
| `id` | The rule ID; required. |
| `status` | `judged`; any other value is refused. |
| `verdict`, `failures` | `pass`, or `fail` with `failures`, each `{file, line?, what}`. |

A failing verdict rejects the held completion as a failed check would, under
the rule's `max_fixes`. Once every rule of the round passed, ww records the
held completion as submitted.

`discover` (a "Rules" section; JSON: `rules_notice`) and the first page of
`start` (JSON: `rules_notice`) say how many declared rules are
`unscriptized` and suggest the `ww-scriptize` skill, which starts
`ww-scriptize-rules`. The notice is left out while
`ww-scriptize-rules` is switched off and when it is the workflow started,
and never blocks anything. `lint` warns with the IDs of those rules and
suggests `ww-scriptize-rules` only while it is switched on.

#### Guiding checks: `rules.check_guidance`

```json
"rules": { "check_guidance": "<free text>" }
```

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `check_guidance` | string | none | The operator's guidance for building checks, carried as written by `rules --json` (`check_guidance`) for `ww-scriptize-rules` and ww's rule-writing skills; blank text is unset. |

`rules` takes no other key; another key, such as the retired `scripting`, or
a non-string `check_guidance` is an error. Every check is recorded by the
operator, through `rules convert`, with `approved_by: operator`; a store
written by an earlier ww may still hold `auto`.

### The rule-automation store

`ww-rule-automation.json` at the project root keeps the project's converted
checks and what is known about each rule wording. It is meant to be
committed; ww writes it under its own lock, only for the operator's `rules`
commands, and leaves it out of every change set. Verification never edits
it, YAML, or rule files; only the operator's `rules` write commands do.

```json
{
  "schema_version": 1,
  "rules": {
    "9f2a…": {
      "text": "Controllers must not instantiate services; inject them.",
      "status": "converted",
      "interpretation": "No `new *Service(` in src/Controller.",
      "check": "deptrac",
      "approved_by": "operator"
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
      "proposed_at": "2026-09-29T11:00:00Z",
      "approved_at": "2026-09-29T11:00:00Z",
      "approved_by": "operator"
    }
  }
}
```

`rules` is keyed by the rule's text hash; ww writes its `status` as
`converted`, `not_convertible` or `rejected`, with `reason` for a rejected or
unconvertible rule. `checks` is keyed by check name; ww writes its `status`
as `converted` or `rejected`, `covers` lists rule hashes, and `reason`
explains a rejected one. Only a `converted` check runs. `approved_by`
(`operator` or `auto`) records who approved an entry. A store written while
verifiers proposed checks inside tasks may also hold the rule statuses
`approach_proposed`, `approach_approved`, `interpreted`, `proposed` and
`ambiguous` (with `approach`, `extends` and `candidates`), the check status
`proposed`, a check's `pending` revision, and `proposed_in`, `proposed_run`
and `approved_in` on either map; ww reads them, judges such rules, and never
writes them any more.
ww reads and writes `schema_version` 1 of the store. An unknown key, status,
or `schema_version` is an error.

### Rule commands

| Command | Effect |
| --- | --- |
| `check <task> [--json]` | Runs the checks of the step in progress against its change set so far, exactly as `complete` would, and prints the failures in the fix page's shape, or `All checks pass`, plus the rules a verifier judges at completion. Records nothing: no attempt counts and no output is kept. Exits 1 when a check fails. Not written to the audit log. |
| `dispute <task> --rule <id> --reason "<why>"` | Only while the step is in progress, and only for an ID a rejected completion of it failed: a rule, a `fix` hook, a derived check, or a judged rule. Stops the task with `operator_reason: check_disputed`; the page shows the check's text, command, and last output, and the worker's reason. |
| `rule <task> <id> [--json]` | One rule or check of the task as its plan froze it: full text, globs, rule file (or the step's own list), command and assertion, `max_fixes`, the steps of the task that carry it, and for a rule without a command what the rule-automation store knows about its wording. |
| `rules [--json]` | The declared root groups with their filters, verifier hints, and rules (ID, summary, globs, whether it has a check, file, times disputed), then each step's own rules and the groups it names. |
| `rules revoke <check> [--reason "<why>"] [--yes] [--json]` | Shows a `converted` or `proposed` store check, asks, and rejects it, recording the reason, together with the rules whose entries name it, which a verifier judges from then on. Never touches YAML, rule files, or the check's config files; the output says they stay for the operator. `--yes` skips the question; without it and without a terminal, it refuses. |
| `rules convert <check> --covers <rule-id>... [--assert empty\|equals:<value>]... [--config <path>...] [--proven] [--dry-run] [--yes] [--json] (--check-shell "<sh>" \| --check-argv <arg>... \| --check-argv -- <arg>...)` | `--check-argv -- <arg>...` goes last and takes every argument after `--` as the argv, options starting with `-` included. Shows the check, its command in full, its config files, the rules it covers with each one's current store state, and every other change, asks, and records it in the store as `converted`, approved by the operator. A new name creates the check; an existing one has its command, config, proof and coverage replaced and any pending revision dropped. A covered rule another check covered moves to this one, and that check loses any pending revision and is removed once it covers nothing more. A rule this check covered before and no longer does returns to unscriptized (its entry is removed); a rejection or decline naming the check stays. `--config` paths are relative to the project and refused when absolute or with a `..` part. Refuses an unknown or repeated rule ID and a rule with a command of its own. `--dry-run` prints the preview and records nothing. `--json` gives the check, its rules, and the `unscriptized`, `moved`, `dropped_checks` and `dropped_revisions` changes. |
| `rules decline <rule-id>... --reason "<why>" [--dry-run] [--yes] [--json]` | Shows the rules with each one's current store state and every other change, asks, and records them as `not_convertible` with the reason, removing them from any check's coverage (a check left covering nothing is removed): a verifier judges them, and `ww-scriptize-rules` leaves them out. `--json` gives the rules and the same changes as `rules convert`. |
| `rules prune [--yes] [--json]` | Lists the store's orphans, rule entries whose wording no declared rule has and checks that cover only such rules and that no remaining rule names, asks, and deletes them. `--yes` skips the question. |
| `rules add <group> --text "<text>" [--paths <glob>...] [--assert empty\|equals:<value>]... [--id <stem>] [--check-shell "<sh>" \| --check-argv <arg>... \| --check-argv -- <arg>...]` | `--check-argv -- <arg>...` goes last, as for `rules convert`. Creates `<stem>.md` in the group's first directory item; the stem is the first five words of the first sentence in kebab-case unless `--id` gives one. Refuses an existing file, a group without a directory, and a group an extension ships. Reports each glob's match count among the project's files. `--assert` is repeatable, one condition each. |
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
store check that runs for a rule without a command of its own; and a
`scriptize` state: `command` (its own check), `converted`, `not_convertible`,
`rejected`, or `unscriptized`, which a rule with no store entry, or one in an
old store's interim status (`interpreted`, `approach_proposed`,
`approach_approved`, `proposed`, `ambiguous`), has. The text listing names the
same state after each rule a verifier judges.

A converted check applies to a step only when every path in its `config`
exists in the directory the step's checks run in, the task's worktree when it
has one. Otherwise its rules are judged for that step (`missing` on the step's
rule resolution names the first absent file, and the step page and the
verifier page say so), so a check whose configuration is still on an unmerged
branch never runs where that configuration is absent. A config path that is
absolute or has a `..` part, which only a hand-edited store holds, counts as
absent.

`ww-rules.yaml`, next to `ww.yaml`, holds only a `rules`
mapping written by `rules add --group` and `rules filter`; ww rewrites it
whole and it is composed like any import. While it exists without being
imported, the write commands refuse to use it.

At a `check_disputed` stop the operator answers with `next`:

| Option | Effect |
| --- | --- |
| `--retry` | The check stands: the dispute is cleared, the rejection keeps counting toward `max_fixes`, and the next `next` hands the step back to its worker. |
| `--force --reason "<why>"` | Waives the disputed ID for this step: its check does not run, or its rule is not verified, when the worker completes again, and the artifact records the waiver. |

At the `fix_limit` stop `--force` waives every check and rule of the step. The
step record keeps its waivers as `checks_waived`, a mapping of ID to reason.
`next --replan` (take the changed workflow definition from the first changed
item on; confirmed when it reruns finished steps) and `next --keep-plan` (carry
on with the saved plan) answer the `plan_changed` stop; see
[features.md](features.md#when-the-workflow-changes-mid-run).
`next --yes` confirms `--retry`, `--force`, or a rewinding `--replan`
without the y/N prompt, for an agent carrying out the operator's stated
decision; the effect is still printed, and the audit record notes the
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

Use `ww-agentic-workflows lint` to validate the complete configuration; it also
warns when `./ww` differs from the launcher this ww writes. Use
`ww-agentic-workflows plan --workflow <name> --agent <agent>` to inspect a
selected workflow's resulting execution plan.
