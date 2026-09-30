# ww feature reference

This document is the developer-facing guide to the features available in
`ww-agentic-workflows`. For a short introduction and one end-to-end example,
start with [README.md](../README.md). For internal design, component boundaries,
and persistence invariants, see [architecture.md](architecture.md).

## Feature overview

- Read-only validation of `ww-agentic-workflows.yaml`, plus agent-specific workflow
  planning in Markdown or JSON.
- A `ww-agentic-workflows.yaml` split across imported files, composed in memory.
- Machine, repo, and local configuration levels, resolved automatically.
- An implicit, reserved `init` step that preserves task requirements.
- Resumable task execution from immutable plan snapshots.
- Agent-owned prompts, skills, slash commands, profiles, and MCP calls.
- ww-owned CLI handlers, output assertions, and lifecycle transitions.
- Global, workflow, and step hooks with filters and conditional prompts.
- Rules delivered on each step's page, and checks ww runs on the files a step
  changed, which send the step back to its worker until they pass.
- Variables, durable task and project metadata, artifacts, and artifact
  dependencies.
- Nested steps, dynamic per-item work, and parent/child task workflows.
- Sequential workflow runs and terminal handoffs between workflows.
- Configurable runtimes, models, reasoning levels, and modes.
- Per-step and per-handler working directories: the task workspace, the
  project checkout, or the project root.
- Explicit manager/worker assignment handoff with durable continuation state.
- Qualified extensions, including bundled Git branch, worktree, and commit
  automation.
- Atomic persistence, task-level concurrency control, interruption recovery,
  execution logs, and lock cleanup.

## Initialize a project

`ww-agentic-workflows init` is additive and safe to run after a partial checkout
or branch switch. It creates only missing files and root configuration keys,
preserves existing content, restores `.ww/tasks`, and reports created and
preserved parts. `--json` and `--no-input` use deterministic defaults for
automation.

The interactive wizard chooses UUID, numeric, or timestamp task IDs. When the
project contains `.git/`, it enables `ww/git`, prefers an existing `master`
branch and then `main`, enables separate task branches, and starts with
`feature/{{task_id}}`. It asks for optional workflow-specific branch formats and
whether worktrees should be used. Enabled worktrees default to
`./git-worktrees/{{task_id}}`, and the directory is created immediately.

The wizard offers to append exactly `../.ww` to `../.gitignore`; without consent it
only reports that action. The patterns `*ww-agentic-workflows.local.yaml` and
`*ww-agentic-workflows.local.json` are added without asking, since [local
configuration](#machine-repo-and-local-configuration) belongs to one checkout:
they are appended to an existing `.gitignore` once, never duplicated, and a
missing `.gitignore` is created for them only inside a Git repository. It also reports missing `@WW_AGENT_INSTRUCTIONS.md`
references in `../AGENTS.md` and an existing `../CLAUDE.md`, and reminds the user to
define workflows when the initialized `../ww-agentic-workflows.yaml` is empty. Equivalent
non-interactive choices are available through `--task-id-format`, `--worktrees`,
`--worktree-dir`, repeated `--branch-format WORKFLOW=FORMAT`,
`--update-gitignore`, and `--skills`.

For every agent directory it finds, such as `.claude/` or `.codex/`, the wizard
offers to install the shipped skills at `<directory>/skills/<name>/SKILL.md`.
The `ww` skill lets a user ask explicitly to work through ww: it tells the
agent to run `discover` and follow ww from there. The `noww` skill is the way
out: invoked as `/noww`, it tells the agent not to use ww for the rest of the
conversation, `catchall` included. The `ww-rule` skill writes rules for ww's
steps from the operator's words; see [Writing rules with the ww-rule
skill](#writing-rules-with-the-ww-rule-skill). `--skills` installs them all
everywhere without asking and `--no-skills` skips them; an existing skill file is never
overwritten, and a skill a later ww version bundles is offered once on its
own, as described in [Installing the ww skills during
init](#installing-the-ww-skills-during-init).

For every hook-capable agent whose directory exists (Claude Code, Codex,
Cursor, Antigravity), init also offers ww's [agent hooks](#agent-hooks), once
per agent, and remembers the answer in `.ww/init-choices.json`. `--hooks`
installs them for all of those agents without asking and `--no-hooks` skips
them; without either flag or a saved answer, a non-interactive init leaves
hooks alone. A hook installation that fails, for example because the agent's
hooks file is not valid JSON, never fails init: the summary names the file and
prints the snippet to add by hand.

### Configuration file names

ww reads its configuration from two files at the project root, both named
after the tool: `ww-agentic-workflows.yaml` for the workflows and
`ww-agentic-workflows.json` for the project settings. Earlier versions called
them `workflows.yaml` and `agentic-workflows.json`. ww does not read the former
names: every command other than `init` stops when it finds one, naming the file
to rename it to.

```console
$ ww-agentic-workflows lint
ww error: found workflows.yaml; rename it to ww-agentic-workflows.yaml, or run init to rename it
```

`init` performs that rename for an existing project, keeping the file's
content, and reports it among the created parts as
`ww-agentic-workflows.yaml (renamed from workflows.yaml)`. When a file exists
under both names, nothing is renamed and ww stops, asking you to remove the
former one, since only the new one is read.

## Discover how to start a task

`discover` is the entry point for agents. The embeddable agent instructions stay
short and send agents here, so every choice comes from the project's current
configuration:

```console
./ww discover
./ww discover --json
```

It lists the workflows and modes with their descriptions and default modes,
the configured projects, the runtimes and roles with what each means, and the
start options with their possible values: agents, runtimes, projects, and the
branch strategies an extension such as `ww/git` defines. It explains when to pass `--model` and `--reasoning`, and
it ends with the exact commands to start a task, show a task's instructions,
check its status, and inspect a workflow's plan. `discover` is read-only and
leaves no audit record.

A task whose state ww cannot read, such as one written by a build whose state
schema this build no longer reads, does not break `discover`. It is listed
under "Unreadable tasks" with the error, and `unreadable_tasks` in the JSON
carries each `task_id` and `reason`. Other tasks and new work are unaffected;
commands addressing that task keep failing with the same error, and whether to
repair, reset, or delete its directory is the operator's decision.

```markdown
## Unreadable tasks

- `TASK-20` — invalid task state .ww/tasks/TASK-20/state.json: unsupported plan snapshot schema_version 14

Other tasks and new work are unaffected. Commands addressing these tasks fail with the error shown; ask the operator, whose choice it is to repair, reset, or delete each task directory.
```

Set `"runtime": "auto"` in `../ww-agentic-workflows.json` to make `auto` the runtime
`start` uses when `--runtime` is omitted; `discover` then marks it as the
default. The setting lives in the JSON file because whether delegation is
available depends on the environment ww runs in, not on the workflows. A
workflow that only makes sense one way, such as a manual-testing workflow
whose every step is a conversation with the operator, may declare
`runtime: single` itself; that outranks the project default, and the flag on
the command line still wins over both.

Set `"enabled": false` in `../ww-agentic-workflows.json` to switch ww off for a
project. `discover` then says only that ww is disabled and that the agent must
not use it, and `start` refuses to create a task.

```json
{"enabled": false, "extensions": {}}
```

## The catch-all workflow

Every change to files goes through ww, including the small ones that fit no
workflow: renaming a helper, fixing a typo, adjusting a setting. For those, ww
provides `catchall` to every project. It has one step, `work`, whose page tells
the agent that the workflow only records the request: it carries the work out
exactly as it would on a plain prompt, with the same judgement, tools,
subagents, skills, and project conventions, and completes the step with what it
changed.

`discover` lists it apart from the configured workflows, under "Changes no
workflow covers", together with the rules for using it, which the agent
instructions repeat. It is only for a change to files: questions,
explanations, reviews, and other read-only work never start a task, and a
conversation that begins as a question turns to `catchall` only once it
reaches a change. It never replaces a matching workflow.

The agent does not start it directly. It first runs `lookup` with the task the
conversation works on, or with what the operator called the task, as they
wrote it, and without one when there is none:

```console
./ww lookup 12345 --agent claudecode
```

`lookup` is read-only. It maps the reference onto the project's task IDs:
the exact ID in any letter case, the ID `task_format` builds from it, so
`12345` and `forms-12345` both mean `FORMS-12345` under `FORMS-{digit}`, and,
failing both, the existing tasks whose ID ends in it after a separator, so
with tracker keys `12345` finds `FORMS-12345`. Then it answers with one next
step:

| Found | Next step |
|---|---|
| One task, with an unfinished run of another workflow | Continue that run: `./ww instruction FORMS-12345 --role manager`. The change belongs to it. |
| One task, otherwise | Start `catchall` on it; the printed `start` command is ready to run. |
| Several tasks | Ask the operator which one, then continue or start on it. |
| No task | Ask the operator to confirm creating the ID the reference names, `FORMS-99` for `99`. |
| No reference | Ask the operator whether to create a new task; under `"task_format": "explicit"` they give its ID. |

Asking goes through the agent's own choice menu, the same mechanism as an
interactive step's [choices](#interactive-steps), and every menu also offers
"Work without ww". Each choice comes with the command to run once it is
picked, and the page says to run nothing before then, so a task ww has never
seen is only created when the operator says so. A `catchall` start on a task
with an unfinished run of another workflow is refused, and the error names
the `instruction` command that continues it.

The workflow declares `runtime: auto` and its step `role: manager`: the
session that received the prompt does the work, and whether it uses subagents
along the way is its own choice, as without ww. It is `restartable`, so a new
request on the same task replaces one that was never finished, and it is not
interactive: a follow-up that changes more starts another `catchall` run on the
same task. Every run ends with the usual workflow summary.

A project replaces the catch-all by defining its own workflow named
`catchall` in `ww-agentic-workflows.yaml`, or switches it off in
`../ww-agentic-workflows.json`:

```json
{"workflows": {"catchall": {"enabled": false}}}
```

## Validate configuration and plan a workflow

`ww-agentic-workflows` turns `../ww-agentic-workflows.yaml` into an explicit, inspectable
execution plan. Skills and slash commands are discovered only from the project's
shared `.agents/` directory and the selected agent's directory.

```console
ww-agentic-workflows lint
ww-agentic-workflows plan --workflow task --agent codex
ww-agentic-workflows plan --workflow task --agent codex --task-id TASK-123 --json
```

The workflow and agent options have `-w` and `-a` short forms. On `start`,
runtime also accepts `-r`.

`lint` validates the complete `ww-agentic-workflows.yaml` configuration without needing a
workflow or agent. It and `plan` are read-only: neither creates task state,
artifacts, commands, or execution log records. Markdown is for people; `--json`
returns a stable representation for tools. `--agent` is required because
automatic resolution depends on the agent’s project-local skills and slash
commands. `--task-id` is optional; when
omitted, `{{__task_id}}` remains visible as an unresolved plan dependency.

## Split ww-agentic-workflows.yaml into several files

A large configuration can be split across files. `ww-agentic-workflows.yaml` stays the
required root file and lists the others under `imports`, its first key (only
`extends` may come before it). Paths are relative to the directory of the file
that lists them; every other relative path in the configuration still resolves
against the project root:

```yaml
# ww-agentic-workflows.yaml
imports:
  - workflows/shared.yaml
  - workflows/local.yaml

handlers:
  - name: test
    argv: [python, -m, pytest, -q]

workflows:
  - task: The standard development workflow.
    steps:
      - develop: Implement the change.
      - test: ~
```

```yaml
# workflows/shared.yaml
handlers:
  - name: test
    argv: [pytest]
workflows:
  - hotfix: Fix a production bug.
    steps:
      - fix: Fix it.
```

```yaml
# workflows/local.yaml
workflows:
  - hotfix: Fix a production bug, verifying it first.
    steps:
      - reproduce: Reproduce it.
      - fix: Fix it.
```

An imported file can define anything `ww-agentic-workflows.yaml` can, except further
imports, so every file is listed in one place. Files apply in order, the root
file last, and a later definition overrides an earlier one of the same name:
here `ww-agentic-workflows.yaml`'s `test` handler replaces the shared one, and
`local.yaml`'s `hotfix` workflow replaces `shared.yaml`'s. Named entries of
`workflows`, `modes`, `documents`, `handlers`, and `profiles` are replaced one
by one, other entries from every file are kept, and `hooks` from every file are
combined, later files' entries running after earlier ones.

Overriding is never an error. `lint` reports each override as a notice:

```console
$ ww-agentic-workflows lint
ww-agentic-workflows.yaml is valid.
Configuration files: workflows/shared.yaml, workflows/local.yaml, ww-agentic-workflows.yaml
Notice: workflow 'hotfix' from workflows/shared.yaml is overridden by workflows/local.yaml.
Notice: handler 'test' from workflows/shared.yaml is overridden by ww-agentic-workflows.yaml.
```

ww composes the files in memory on every command into one document and reads
it exactly as a single `ww-agentic-workflows.yaml`, so every other rule applies unchanged
and there is no cache to refresh. `init` sees keys and workflows defined in
imported files too, and does not add them to `ww-agentic-workflows.yaml` again. The
[specification](specification.md#imports) has the exact rules.

## Machine, repo, and local configuration

Both configuration files come in three levels, which ww finds and applies on
every command, top to bottom:

1. machine: `ww-agentic-workflows.machine.yaml` and
   `ww-agentic-workflows.machine.json` in `~/.config/ww-agentic-workflows/`
   (under `$XDG_CONFIG_HOME` when that is set), shared by every project on the
   machine;
2. repo: `ww-agentic-workflows.yaml` and `ww-agentic-workflows.json` in the
   project root, checked in;
3. local: `ww-agentic-workflows.local.yaml` and
   `ww-agentic-workflows.local.json` in the project root, for one checkout and
   kept out of version control.

The repo `ww-agentic-workflows.yaml` stays required: a machine file alone never
makes a directory a ww project. `WW_MACHINE_CONFIG_DIR` names another machine
directory; the test suite points it at an empty one so a developer's own
machine configuration never leaks into tests.

A lower level extends the levels above it with the same rules as
[imports](#split-ww-agentic-workflowsyaml-into-several-files), and wins: named
workflows, modes, documents, handlers, and profiles are replaced one by one,
hooks are added per phase, and other keys take the lower value. Each level can
use `imports` of its own, resolved next to the file that lists them.

```yaml
# ~/.config/ww-agentic-workflows/ww-agentic-workflows.machine.yaml
handlers:
  - name: test
    argv: [pytest]
workflows:
  - review: Review a change.
    steps:
      - review: Review it.
```

```json
// ww-agentic-workflows.local.json
{"task_format": "DEV-{digit}"}
```

With the repo file defining its own `test` handler and its
`ww-agentic-workflows.json` a `task_format`, the project gets the machine's
`review` workflow, the repo's `test` handler, and the local task format. `lint`
lists the files it read, machine to local, the YAML files first and the JSON
ones after, and names the file behind each YAML override; `plan` ends with the
same list:

```console
$ ww-agentic-workflows lint
ww-agentic-workflows.yaml is valid.
Configuration files: ~/.config/ww-agentic-workflows/ww-agentic-workflows.machine.yaml, ww-agentic-workflows.yaml, ww-agentic-workflows.json, ww-agentic-workflows.local.json
Notice: handler 'test' from ~/.config/ww-agentic-workflows/ww-agentic-workflows.machine.yaml is overridden by ww-agentic-workflows.yaml.
```

A configured project's own `ww-agentic-workflows.json` and
`ww-agentic-workflows.local.json` add one more level for tasks working there,
limited to the `extensions` section and `task_format`; see
[A project's own extension settings](#a-projects-own-extension-settings).

A level that should not build on the ones above sets `extends: false` in its
root file or any of its imports; that level then starts afresh, and `lint`
reports each file it leaves out. `extends: true` is allowed and changes
nothing.

```yaml
# ww-agentic-workflows.local.yaml
extends: false
workflows:
  - task: My own way of working.
    steps:
      - develop: Do it.
```

```console
$ ww-agentic-workflows lint
ww-agentic-workflows.yaml is valid.
Configuration files: ww-agentic-workflows.local.yaml, ww-agentic-workflows.json
Notice: ~/.config/ww-agentic-workflows/ww-agentic-workflows.machine.yaml is not applied: a lower level sets extends: false.
Notice: ww-agentic-workflows.yaml is not applied: a lower level sets extends: false.
```

The JSON settings are always deep-merged and take no `extends` key: nested
objects merge key by key, while strings, numbers, booleans, lists, and `null`
from a lower level replace the value above. A machine file can, for example,
set `{"runtime": "auto"}` for every project while one checkout's
`ww-agentic-workflows.local.json` sets
`{"extensions": {"ww/git": {"worktrees": false}}}` without repeating the rest of
the repo's `ww/git` settings.

`init` writes only the repo-level files, and decides what to add from the
composed result: it adds no default workflow to the YAML, and no `task_format`
to the JSON, when another level already provides them.

## Configuration

Every mode is a mapping with a required `name` and an optional description.
Workflows, handlers, and steps support that long form plus a shorthand whose
first key is the name and whose string or null value is the description.
`handlers` is the reusable global catalog. A step is also a handler, with
optional lifecycle hooks.

```yaml
handlers:
  - update-yaml-specification: Update specification.md.
  - no-description: ~

workflows:
  - task: The standard development workflow.
    steps:
      - develop: Implement and test the change.
```

A step can copy a root handler definition with `handler: <name>`, and a step
that has no content of its own, `- fetch_requirements: ~`, copies the root
handler of the same name without saying so, exactly as a bare hook entry does;
settings such as `profile` or `model` on that step still override the copy.
This is a hard link at configuration time, not another execution phase: the
plan contains one ordinary step under the step's own name. Its description and
explicitly declared handler fields override the copied definition. Root handlers may also
hold a complete step container, so a loop or nested step sequence can be named
once and reused by referencing steps.

```yaml
handlers:
  - name: shared-check
    argv: [pytest, -q]

workflows:
  - name: task
    steps:
      - verify: Run the project verification.
        handler: shared-check
```

A reusable handler can contain the whole step structure. For example, define a
review loop once and use it as a workflow step:

```yaml
handlers:
  - code-review:
      loop:
        - code-review: Perform the code review.
          stop: There are no meaningful review remarks.
          profile: code-reviewer
        - fix: Fix the review findings.
          profile: developer

workflows:
  - name: task
    steps:
      - code-review: ~
        handler: code-review
```

The same shorthand works for singular hook actions and entries inside a hook's
ordered `handlers` list. If a mapping contains `name`, ww preserves the existing
long-form interpretation.

### Profiles

Profiles tailor the agent's approach without implying that another agent will
be spawned. Define them at the root and apply one to a workflow or one of its
steps. A profile is inherited along the same chain as agent, model, and
reasoning: from the workflow, through every enclosing step, to the step itself,
so a loop wrapper or a parent step sets it for its whole body and any nested
step may override it. Hooks, modes, and handlers cannot declare profiles.

```yaml
profiles:
  developer: >-
    You are a senior Symfony developer. Prefer small, well-tested changes.
  code-reviewer: ~

handlers:
  - name: git-stage
    argv: [git, add, .]
  - name: git-commit
    provide:
      - name: commit_message
        description: A concise commit message.
    argv: [git, commit, -m, "{{__task_id}}: {{commit_message}}"]

hooks:
  before_complete:
    - steps: [develop]
      handlers:
        - name: git-stage
        - name: git-commit

workflows:
  - name: task
    steps:
      - name: develop
        description: Implement and verify the requested change.
        profile: developer
```

### Implicit requirements step

Every workflow begins with an artifact-producing `init` step supplied by ww.
Do not declare it in `steps`: `init` is a reserved step name, including inside
nested and per-item steps. Its fixed prompt asks the agent only to preserve the
passed requirements, correcting grammar and style without adding analysis,
reasoning, or a work plan. It uses low reasoning and does not inherit a workflow
profile, keeping the saved requirements independent of later execution choices.

`init` otherwise has the same lifecycle and persistence behavior as a declared
step. The workflow-wide `before_start_workflow` boundary runs once before it and
does not accept a step filter:

```yaml
hooks:
  before_start_workflow:
    - name: check-clean

workflows:
  - name: task
    steps:
      - name: develop
        depends_on: init
```

Each workflow run gets its own `init`, including handoff targets and child-task
workflows. `ww plan` includes it in the exact execution order. `start` requires
`--init-artifact` and stores that normalized requirements text immediately in
the built-in artifact before returning the first declared step; it never assigns
`init` to a worker or requires a subsequent `next`/`complete` cycle.

### Commands

Command handlers canonically put a structured argument vector at their root,
for example `argv: [git, status, --porcelain]`. Each argument is interpolated
independently and execution never invokes a shell.

When shell behavior is intentional, declare it explicitly. Shell source is not
interpolated; dynamic workflow values must enter through `env` or `args`:

```yaml
shell: printf '%s' "$MESSAGE" > message.txt
env:
  MESSAGE: "{{commit_message}}"
```

`assert` adds an `eq` output assertion to the root command action. A handler is
one action; use a hook's ordered `handlers` list for multiple commands.

`idempotent: true` declares that running the handler again is harmless. It
changes one thing: when ww is interrupted while the handler runs, the next
locked `next` replays the interrupted and unrun commands under the same
operation identity and carries on, instead of stopping at the recovery
boundary for an operator decision. The default is `false`, which keeps the
unknown outcome until `next --retry`, `recover --mark-succeeded`, or a checker
settles it; see [Interrupted automatic handlers](#interrupted-automatic-handlers).
Declare it on test runs, linters, and checks that only read; leave it off
anything that publishes, commits, or sends. `lint` rejects it without `argv`,
`shell`, or `command`, the saved plan carries it, and `plan` shows it under
**Recovery**:

```yaml
handlers:
  - name: tests
    argv: [python, -m, pytest, -q]
    idempotent: true
```

### Handler types and ownership

Set `skill: true`, `slash_command: true`, `mcp: <connection>`, `argv`, `shell`,
or `prompt: true` to select a handler kind explicitly. The handler description
is the instruction for prompt and MCP work. `prompt` accepts only `true`; use
an `assess` step when a decision must control workflow routing.
Without an explicit action, resolution is deterministic:

1. A matching project-local skill is an agent skill handler.
2. A matching project-local slash command is an agent slash-command handler.
3. Otherwise the name/description becomes an agent prompt.

### Outcome-based assessments

Use `assess` when an agent's judgment should route the remainder of a workflow.
The agent should choose `positive` or `negative` whenever possible, reserving
`mixed` for material uncertainty. After completing the assessment, select the
declared route with `ww next <task-id> --outcome <label> --role manager`.

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

Every outcome uses one ordinary step shape: a global `handler`, an inline
action, `handlers`, or nested `steps`. Outcome work types are mutually
exclusive. After the chosen outcome's work, the workflow continues with the
step after `assess`. An outcome that should end the run instead is
`stop_workflow: true` on its own:

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

Choosing it completes the workflow, skipping every later step and hook. An
assessment needs at least one outcome with work; one that only stops is the
compact form.

A gate declares only the outcome that has work. `positive`, `negative`, and
`mixed` are accepted whether declared or not, and an undeclared one runs nothing
and continues after the assessment:

```yaml
- assess:
    question: Were conflicts resolved in non-trivial code?
    outcomes:
      positive:
        steps:
          - review: Review the resolutions.
- verify: Run the tests.
```

Here `negative` and `mixed` go straight to `verify`. A label of your own, such
as `partial`, is accepted only when declared. For a simple gate, `- assess: <question>` accepts `positive` or
`negative`; positive continues normally and negative completes the workflow.

The agent sees the choice before it answers: the assessment's page lists each
outcome and what it does, for example "`negative` — ends the workflow here".
Once the assessment is complete, the next page asks for the outcome and shows
one `next --outcome <label>` command per outcome, never a plain `next`, which
ww would refuse. A delegating manager chooses it itself; no worker preview is
shown until the outcome decides which work comes next.

`profile` may be a name or a mapping containing `name` and/or `description`.
The mapping form supplies an inline description. ww resolves a named profile by
first checking the selected agent directory's `agents/` folder (for example,
`.codex/agents/developer.md`), then using the inline or root `profiles` text,
and finally instructing the agent to use the profile by name when it has no
description. A discovered file is always named explicitly in the instruction.

CLI handlers and workflow transitions are owned by `ww`; skills, slash commands,
and prompts are owned by the agent. The plan exposes this ownership for every
item. It also exposes an explicit execution mode:

- A CLI handler is `automatic`: ww runs it. It is shown in the plan for
  information and an agent must not run its command directly.
- A CLI handler with `provide` is still automatic, but first has
  `requires_agent_input: true`. The preceding agent completion instruction
  collects those values and then ww runs the command itself.
- Skills, slash commands, prompts, and MCP prompts are `agent_instruction`
  items.
- A workflow transition is a `workflow_transition`, owned by the coordinator but
  not an automatically run CLI command.

If an agent-owned item cannot be completed, its worker uses
`ww fail <TASK-ID> --role worker --error "<functional error>"`. The task enters
a failed state. The worker reports it to the manager, and the manager reports it
to the ww operator for manual intervention.

An artifact-enabled workflow step must be completed with a non-empty
`--artifact`. Set `artifact: false` on a step that has no useful durable result.
This prevents a successful-looking completion from silently losing the agent's
work.

## Modes and execution settings

Modes add reusable guidance to a workflow. Define defaults on the workflow and
add more when starting a task:

```yaml
modes:
  - name: economy
    description:
      - Keep responses concise.
      - Prefer lighter reasoning where appropriate.

workflows:
  - name: task
    modes: [economy]
    model: gpt-5
    reasoning: high
    steps:
      - name: implement
        model: gpt-5-mini
```

```console
ww-agentic-workflows start TASK-123 --workflow task --agent codex --role manager \
  --init-artifact="Implement the requested change." \
  --mode economy --runtime auto --model gpt-5 --reasoning high
```

`single` uses one session for manager and worker responsibilities. `auto`
lets a manager dispatch assignments to separate workers; it does not make `ww`
launch models. Both runtimes use the same command exchange and persisted state.
Workflow and step `model` and `reasoning` values are recorded with the work, and
step values override workflow values. Use `modes`, `runtimes`, and `agents` to
inspect the available choices.

### Steps the manager performs

`role` says who performs a step: `worker`, the default, is delegated in the
`auto` runtime; `manager` keeps the step in the managing session in every
runtime, such as a review whose judgement the manager keeps for itself. The
step's profile, agent, model, and reasoning settings are deliberately ignored
for a manager's step, whether set on it or inherited, and `lint` notes them.
The manager's pages say so: the dispatch page states that no worker is
selected and shows a plain `next`, and the page after it tells the manager to
perform the step itself, with no worker bootstrap. An interactive step is
always the manager's, since only its session can talk to the operator.

`role` is inherited like `profile`: set on a workflow, a group of steps, or a
loop wrapper, it applies to everything inside, and a nested step overrides it.
A step ww runs itself, such as a command, has no role; setting one on it is an
error, while an inherited role passes over it.

```yaml
workflows:
  - name: task
    model: gpt-5
    steps:
      - develop: Implement the change.
      - name: review-loop
        role: manager
        loop:
          - review: Review the diff and list the findings.
            break: There are no findings.
          - fix: Apply the findings.
            role: worker
```

### Steps without subagents

`subagents: false` says that whoever performs a step does all of its work
alone: no subagent for research, tests, review, or anything else. It is
independent of `role` and of the model: a delegated step with its own model can
forbid helpers, and so can a step the manager performs. The step's page states
the rule in its work instruction. It is inherited like `profile`, so on a group
it covers every step inside, and a nested step may set `subagents: true` again.

```yaml
- name: develop
  model: opus
  subagents: false        # nobody working on develop's steps spawns subagents
  steps:
    - research: Find what the change touches.
    - implement: Implement it.
      model: sonnet       # its own model, still no subagents
```

Before `role` existed, `subagents: false` meant that the manager performed
the step. That meaning is `role: manager` now; YAML that used `subagents:
false` for it must say `role: manager` instead.

## Manager and worker assignments

Caller role describes responsibility for one command. It is explicit workflow
coordination rather than authentication. The manager owns `start`, `next`, and
recovery. A worker may inspect status and complete or fail active work. Omitted
roles retain the previous command behavior for existing scripts and service
callers, while every newly generated execution command includes a role.

One manager `next` dispatches a structural assignment. For a leaf step, that
assignment contains its preparation hooks, main action, and completion hooks.
Each worker completion records one agent result, runs eligible automatic work,
and activates the next agent hook in the same assignment. The response exposes
`continue_worker`, `handoff_manager`, `blocked`, or `awaiting_operator`
together with `next_role`. `blocked` means ww is waiting on its own work, such
as a child workflow, a loop boundary, or a running automatic handler, and the
manager continues. `awaiting_operator` means a human must decide; see
[Awaiting the operator](#awaiting-the-operator).
At handoff only the manager runs the displayed `next --role manager` command.
In the `auto` runtime the manager's delegate page and the requested worker
describe the step that drives selection, not whichever hook the cursor is on,
and both the manager and the worker are told every item the assignment covers.
A worker moving to a later item of the same assignment is told so explicitly,
and the end of an assignment says to stop and return to the manager.

An assignment that holds no agent step, only an automatic handler waiting for
values, such as a commit hook that needs its message after a loop boundary, is
not delegated. The manager has just read the outcome it would summarize, so
the `awaiting_input` instruction is addressed to the manager, its command
carries `--role manager`, and the preview before it says that no worker is
selected. Every pending-input page also lists, under "Work these values
describe", the handovers of the steps completed since that handler last ran
in the run, so a commit message names this round's work rather than repeating
an earlier one. Every worker instruction also repeats the requirements saved by
`init` under "Task requirements", so the user's wording reaches each worker
without the manager adding commentary, and states that ww writes the artifact
from the completion command, never the worker under `.ww`. It then shows, under
"Previous step result", the handover of the step completed most recently
before this one. Completing an ordinary step requires
`--summary-for-next-step`, one or two sentences on what was done and what the
next step must know; ww stores it on the step record and shows it to the next
step together with the artifact's path, so the full result stays in the
artifact and is read only when the summary is not enough. Hooks, `init`,
and the built-in summary do not take one. Only ordinary steps count: hook results,
the built-in summary, and `init` are never chosen, and the history of earlier
loop rounds is included, so the first step of a later round sees the previous
round's last step. `depends_on` remains the way to point a step at a specific
earlier artifact when the immediately previous one is not the right input.

An active step's Markdown instruction also names its later sibling steps. This
gives the worker a lightweight scope boundary without repeating those steps'
descriptions or prescribing a strict prohibition:

```markdown
Avoid duplicating work that is better handled by the next steps.

### Next steps

- run-tests
- check-code-quality
```

The JSON instruction exposes the same ordered names in `next_steps`. Hooks do
not receive the list, and a step with no later siblings omits the section.

Parent-only preparation hooks form their own dispatch before descendants.
Completion hooks on a parent trail the last descendant, and the built-in final
summary trails the final assignment. A newly materialized item, child-workflow
coordinator, workflow transition, or successor execution instance is always a
manager boundary.

On a failure or an interruption, the response reports `awaiting_operator`
and states whether a preceding worker result was already saved.
`instruction --role worker` reconstructs the same continuation or handoff from
persisted state after a restart. Caller roles do not add stale assignment
tokens or change the existing concurrency guarantees.

### Assignment tokens

In the `auto` runtime every assignment the manager hands out carries a short
token. The bootstrap command gives it to the worker, and every command a
worker page prints repeats it:

```console
./ww instruction TASK-7 --run 01-task --role worker --assignment 3f9a1c07
./ww complete TASK-7 --role worker --assignment 3f9a1c07 --artifact="..." --summary-for-next-step="..."
```

`instruction`, `complete`, `loop`, `fail`, `interact`, and `dispute` with
`--role worker` accept a call only when its token is the open assignment's,
so a worker whose assignment ended, or that holds another assignment's token,
cannot act. When the assignment ends, at a handoff to the manager, a return
from a loop, or a failure, ww closes the token. A worker command after that is
refused with "your assignment has ended", and one without a token is sent back
to the manager. While no assignment is open, a worker's `instruction` still
answers with the page that only sends it back.

The manager needs no token, and it never has to remember one. After a context
compaction, `instruction <task> --role manager` shows the open assignment and
its bootstrap command with the same token, so a worker still holding it
carries on. `next <task> --role manager --reassign` issues a new token for the
open assignment and closes the old one, for a worker that was lost or must be
replaced. A step the manager performs itself, `role: manager`, is an assignment
of its own with its own token, which no worker page ever shows.

Tokens guard against a confused agent, not a hostile one: a worker that runs
the manager's commands is still not stopped. The `single` runtime, where one
session does every step, uses no tokens.

### Confirmations

`next --retry`, `next --force`, `next --approve`, and `rules prune` ask the
operator to confirm, because they can repeat an external effect, skip work, or
approve a command that will run from then on. ww asks only at a terminal. An
agent's shell has none, so there ww refuses at once and names `--yes`, and it
never reads an answer from a pipe. The pages that show these choices print
them with `--yes`, since the agent runs one only after the operator chose it;
the effect is still printed, and the task's audit record notes whether the
operator confirmed at a terminal or an agent did with `--yes`.

### Awaiting the operator

The operator is the human running the agent. When only they can decide how a
task goes on, the response says so in a machine-readable way instead of in
prose: `control` is `awaiting_operator`, `next_role` is `operator`, and
`operator_reason` says why.

| `operator_reason` | When |
| --- | --- |
| `handler_failed` | An automatic handler failed. |
| `work_failed` | An agent step was recorded with `fail`, or the bootstrap failed. |
| `child_failed` | A child task failed. |
| `interrupted_command` | An automatic handler was interrupted and its outcome is unknown. |
| `loop_limit` | A loop reached its `loop_max_times` iteration limit. |
| `fix_limit` | A step's check failed as many times as its `max_fixes` allows; see [Rules and checks](#rules-and-checks). |
| `check_proposed` | Verifiers proposed how to check a step's rules, and the operator decides; see [How a rule becomes a check](#how-a-rule-becomes-a-check). |
| `check_disputed` | A step's worker disputed a check that rejected its completion; see [Checking early and disputing a check](#checking-early-and-disputing-a-check). |

An interrupted handler declared `idempotent: true` is not a reason: `next`
replays it without asking anyone, so the task stays `blocked` for the manager.
`operator_reason` is `null` whenever `control` is anything else. The same three
fields appear in `instruction` and `status` JSON:

```json
{
  "control": "awaiting_operator",
  "next_role": "operator",
  "operator_reason": "handler_failed"
}
```

Markdown heads the page with the decision, for example
`## Operator decision: the automatic handler failed`, and tells the agent to
stop and ask the operator. The recovery commands, `next --retry` and
`next --force --force-reason`, are still shown, as the operator's choices; the
agent runs one only after the operator picks it. A delegated worker is not told
to ask anyone: it returns to its manager as before, and the manager asks.

The operator is someone ww waits for, not a caller: `--role` still accepts only
`manager` and `worker`, and `--role operator` is rejected.

## Lock cleanup and command attempts

ww keeps lock sidecar paths in `../.ww/locks`; a file there is not evidence of a
currently held lock. Remove inactive sidecars with:

```console
ww-agentic-workflows cleanup
```

Cleanup waits until active and waiting ww operations have left their lock gate,
then removes the old sidecars safely. The execution log now records `started`
before a command runs and records its final `ok` or `error` result afterwards,
so an interrupted operation is visible as a `started` entry without a terminal
record.

## Projects: one ww instance over several repositories

Projects are optional. Without them a task works in the project root, where
`ww-agentic-workflows.yaml` and `.ww` live. With a `projects` list in
`../ww-agentic-workflows.json`, that root can be a workspace directory above
several repositories, and each task or child chooses the repository it works
in. Projects live in the JSON settings rather than in `ww-agentic-workflows.yaml` because
their locations are machine-specific, while the workflows are shared:

```json
{
  "projects": [
    {"name": "backend", "path": "./backend", "description": "Python API service."},
    {"name": "frontend", "path": "./frontend"}
  ]
}
```

| Key | Required | Meaning |
| --- | --- | --- |
| `name` | yes | Unique normalized name; the value of `--project`. |
| `path` | yes | The directory, resolved against the root when relative. It must exist when a task starts there. |
| `description` | no | Shown by `discover` and in the children collection step. |

A checkout laid out differently can list its own projects in
`ww-agentic-workflows.local.json`; the list replaces the repo's whole, and its
relative paths still resolve against the root.

```console
ww-agentic-workflows start PROJ-123 --workflow feature --project backend ...
ww-agentic-workflows add-child EPIC-1 --description "Web part" --project frontend
```

The chosen project's directory becomes the task's working directory: commands
and hooks run there, `{{__task_workspace_dir}}` points at it, `{{__project}}`
holds the name, `{{__project_dir}}` the directory, and `{{__projects}}` lists
every configured project name, joined by commas. `ww-agentic-workflows projects` prints the list as JSON.
Configuration, state, and artifacts stay in the root, so one
task's requirements, plan, and reviews are kept together even when its children
touch several repositories. `discover` lists the projects, and the children
collection step lists them so the agent can pass `--project` per child. A handoff
successor run keeps its project. Anything without `--project` behaves exactly as
before.

The `ww/git` extension follows the working directory: branches, worktrees, and
commits act on the repository the task works in, and a task in a worktree still
resolves to that repository's primary checkout. A repository whose conventions
differ from the root's states them in its own settings file, described next.

### A project's own extension settings

A configured project may carry `ww-agentic-workflows.json` and
`ww-agentic-workflows.local.json` in its own directory. Of those files ww reads
exactly two keys, repo file then local file: the `extensions` section, applied
over the root's effective section for the same extension with the same rule as
between configuration levels (nested objects merge key by key, any other value
replaces the root's), and `task_format`, which replaces the root's for tasks
started in that project. A project therefore states only what differs:

```json
{
  "task_format": "WEB-{digit}",
  "extensions": {
    "ww/git": {
      "base_branches": {"default": "master"},
      "commit_format": "[{{task_id}}] {{commit_message}}",
      "worktrees": true,
      "worktree_dir": "../frontend-worktrees"
    }
  }
}
```

With that file, `start --workflow feature --project frontend` without a task
ID generates `WEB-1`, `WEB-2`, and so on, and `add-child ... --project
frontend` without `--id` names the child the same way under its parent, while
tasks started without `--project`, or in a project that sets no format, keep
the root's. A project may set `"task_format": "explicit"` to require an ID for
its tasks while the root generates them, and the other way round. `lookup`
resolves references with the root's format. `discover` names a project's
format after its entry when it has one of its own.

Every other key of a project's file (`enabled`, `runtime`, `executable`,
`projects`, `workflows`, and anything else) is ignored here: those describe
the project as a ww root of its own, which it may also be when used on its
own, and the workspace root owns them. A project file never marks a ww root,
and a project without such files, or a task started without `--project`, gets
the root's settings unchanged. The root stays the only place that decides
which extensions are configured: a project section naming an extension that is
not installed is an error that names the project's file, for example
`frontend/ww-agentic-workflows.json (project 'frontend') configures unknown
extension 'acme/notes'`.

The settings follow the directory a step or hook acts on, not the task as a
whole, and are frozen into the plan when the task starts like every extension
setting. An item working in the task workspace or the project directory
(`workdir` `task` or `project`) gets the project's settings; one working in the
root (`workdir: root`) gets the root's. Relative paths inside a project's
section, such as `worktree_dir`, resolve against that project's repository,
never the workspace root. Generated task IDs also reserve a project's worktree
paths under the project's settings when the task starts with `--project`.

`lint` validates every project's sections and, like `plan`, lists the project
files it read after the root's:

```console
$ ww-agentic-workflows lint
ww-agentic-workflows.yaml is valid.
Configuration files: ww-agentic-workflows.yaml, ww-agentic-workflows.json
Project frontend extension settings: frontend/ww-agentic-workflows.json, frontend/ww-agentic-workflows.local.json
```

`plan --project <name>` compiles a workflow as a task in that project would
get it, `extension ww/git settings --project <name>` prints the settings such
a task's handlers receive, and `discover` names each project's branch
strategies when they differ from the root's, and its task ID format when it
has one.

One consequence of per-project worktrees: run ww through the root launcher
`./ww` or with `--root`. Invoked from inside a project's checkout or worktree
without either, ww resolves the root through the Git common directory to that
project's own checkout, not to the workspace.

### Choosing where a step works

By default every step and hook works in the task workspace: the Git worktree
when one was selected, otherwise the `--project` directory, otherwise the
project root. Some work belongs elsewhere, such as updating shared or
git-ignored files in the root checkout rather than in a task worktree. Set
`workdir` on a step or on a handler to choose its directory:

| Value | Directory |
| --- | --- |
| `task` | The task workspace; the default, unchanged when `workdir` is omitted. |
| `project` | The `--project` directory's own checkout, never the worktree made from it; the project root for a task without `--project`. |
| `root` | The project root, where the configuration and `.ww` live. |

```yaml
handlers:
  - name: refresh-shared-config
    argv: [make, shared-config]
    workdir: root

workflows:
  - name: feature
    steps:
      - develop: Implement the change.
      - update-local-notes: Record the decisions in {{__task_workspace_dir}}/notes/.
        workdir: root
        hooks:
          after_complete:
            - refresh-shared-config: ~
            - name: lint
              argv: [make, lint]
              workdir: project
```

An extension handler entry, such as `- ext/ww/git/handlers:git-commit: ~`,
may carry `workdir` as well, and nothing else: the extension defines the rest.
For `update-local-notes`, the instruction's working-directory `cd` names the
root and `{{__task_workspace_dir}}` resolves to it; an `argv` or `shell` step
runs its command there. Nested steps, loop bodies, and per-item stages inherit
the value from their enclosing step and may set their own. A hook does not
inherit its step's directory: it uses its own `workdir`, then that of the root
handler it names, and otherwise the task workspace, so above
`refresh-shared-config` runs in the root and `lint` in the `--project` checkout.

ww does nothing special with Git for such a step: its changes are not part of
the task's commits and are left for the operator.

## External task IDs

An MCP-backed first declared step can establish the task identity without a new
Jira-specific setting. When `start` is called without a task ID and that step
provides exactly `task_id`, ww starts it as a short bootstrap request before the
implicit `init`. Complete it with the ID returned by the tracker; only then does
ww create `.ww/tasks/<external-id>/`, run normal start hooks and `init`, and continue
the rest of the workflow.

```yaml
workflows:
  - name: jira-task
    steps:
      - name: create-jira
        mcp: jira
        description: Create the Jira issue and return its key.
        provide:
          - name: task_id
      - name: develop
        description: Implement {{task_id}}.
```

The bootstrap step must be the first declared flat workflow step and cannot have
hooks, nested work, or other provided values. Its artifact is retained with the
task's run artifacts. If an ID is passed explicitly to `start`, it is
authoritative: ww supplies it as `{{task_id}}` and never accepts a replacement
from an agent.

### Children that bind their own IDs

The same step lets each child of a parent task obtain its own external ID, for
example one Jira story per child of an epic. When the child workflow's first
step provides `task_id`, the parent's collection step tells the agent to record
children without `--id`; ww names each one by a temporary request ID until it
starts:

```console
ww-agentic-workflows add-child EPIC-1 --description "Story one" --project backend
ww-agentic-workflows child start EPIC-1 REQUEST-20260923101500123456
ww-agentic-workflows next REQUEST-20260923101500123456 --role manager
ww-agentic-workflows complete REQUEST-20260923101500123456 --role worker \
  --variable task_id=PROJ-456 --artifact "Created PROJ-456."
```

`child start` opens the identity request instead of a run, and the parent's
instruction points at it while it is in progress. Completing the request with
the tracker's key creates the child as `EPIC-1/PROJ-456` in its project
directory, with the identity step already done, and renames the parent's child
record. Each story is therefore created by its own step with its own retry and
failure handling: a crash halfway through the split can no longer leave stories
without children, and a retried request that already bound its child simply
reports the bound task. A child added with an explicit `--id` skips the request,
as an explicit ID does for `start`.

## Inheriting a workflow

A workflow that should do exactly what another does, but branch or merge
differently, inherits it instead of repeating it:

```yaml
workflows:
  - hotfix: Fix a bug on main.
    steps:
      - investigate: Find the cause.
      - fix: Fix it.
  - bugfix: Fix a bug on dev.
    inherit: hotfix
```

`bugfix` gets `hotfix`'s steps, workflow hooks, modes, runtime, and every other
setting. Its own keys replace the copied ones, so it can change its
description, runtime, or recommendation; it cannot declare `steps` or `hooks`,
because a workflow with other steps is a workflow of its own. A global hook
filtered to `workflows: [hotfix]` also runs for `bugfix`, and for anything that
inherits `bugfix` in turn. Only the name differs, and that is the point:
`ww/git` reads `branch_name_formats.bugfix` and `base_branches.bugfix`, so the
copy branches from and names its branches after its own entries. `discover`
marks the copy with "Same steps as `hotfix`."

## Recommending the next workflow

A workflow that is usually followed by another names it:

```yaml
- hotfix: Fix a bug on main.
  recommended_next_workflow: merge-to-dev
  steps:
    - fix: Fix it.
```

When a `hotfix` run completes, its page tells the agent not to start anything
on its own but to ask the operator, through its choice menu, whether to start
`merge-to-dev` on the same task, and shows the `start` command to run if they
agree. Nothing starts without that answer. An inheriting workflow keeps the
recommendation unless it sets its own, or `recommended_next_workflow: ~` to
clear it. A `handoff` workflow already starts its successor and cannot
recommend one.

## Rules and checks

A **rule** is a sentence a step's agent must follow, such as "Keep the public
CLI unchanged." A **check** is evidence ww collects itself: a command it runs
when the step completes. A rule may carry its own check, and a
`before_complete` hook with `on_failure: fix` is one too. The agent that did
the work never grades it: a claim that can be a command is run by ww.

Rules live in Markdown files grouped under the root `rules`, or inline in a
step's `rules` list; the [specification](specification.md#rules) has the
format. A group applies where its `workflows` and `steps` filters allow (each
`"*"` or a list of names, as on hooks), and a step may name a group to get it
regardless.

```yaml
rules:
  python: [rules/python/]
workflows:
  - name: task
    steps:
      - name: develop
        description: Implement it.
        rules:
          - Keep the public CLI unchanged.
        hooks:
          before_complete:
            - argv: [pytest, -q]
              on_failure: fix
```

### On the step page

Every agent step lists its rules after the work instruction, each with its ID,
its globs, and its first sentence; the IDs of rules with a check are collected
on one line, "Checked automatically when you complete". The section names
`ww check <task>`, to see the checks' result at any time without completing,
and `ww rule <task> <id>`, to read a rule in full. The page asks the
worker to say in its artifact, under a **Rules** heading, which rules it
applied and any deviation. `init`, hooks, and the workflow summary get no
rules. In the `auto` runtime the worker's page carries the section. JSON
output lists them as `rules`, each with `id`, `summary`, `paths`,
`has_command`, `hook`, `check`, `interpretation`, and `pending_operator`.

A rule without a check is judged after completion by a verifier, never by the
worker; the page says so. A rule whose wording already has an approved
derived check is listed with the checked ones, and a judged rule shows the
store's interpretation under it, plus a note while a proposal for it waits
for the operator.

### The fix loop

`complete` runs the step's checks before recording anything. When one fails,
the completion is rejected: nothing is saved, the artifact is kept only as a
draft, the step stays in progress with its worker, and `complete` exits
non-zero. The response is the fix page, `## Fix required: 2 of 5 checks failed
(attempt 1 of 3)`, with each failed check's rule text, command, and the last 40
lines it printed, then the same completion command; `fix_required` in JSON.
The worker fixes the causes and completes again with a revised artifact.

A failed check always goes back to the worker, never to the operator, until
it has failed `max_fixes` times: the rule's own value, else `max_fixes` in
`ww-agentic-workflows.json`, default 3. Then the task stops with
`operator_reason: fix_limit` and the last failures on the page. The operator
chooses:

- `next --retry` gives the worker another round: the count starts again, and
  the next `next` hands the step back.
- `next --force --force-reason "<why>"` waives the checks: the worker completes
  the step once more without them, and its artifact records the waiver.

Both ask for confirmation; `next --yes` confirms for an agent that carries out
what the operator said.

A hook without `on_failure: fix` fails as before, stopping the task with
`operator_reason: handler_failed`.

### The change set

A check sees the files the step changed in `WW_STEP_CHANGED_FILES`,
newline-separated and relative to the step's directory, narrowed to its
`paths`; a check whose globs match none of them is not applicable and does not
run. ww measures the change set with git: when the step begins it records the
tree of everything in the working directory, tracked or not, using a temporary
index, so the real index, the stash, and the files are untouched; at
completion it takes a second tree and compares. Work that was uncommitted
before the step cancels out, a commit made during it still counts, and
deleted files, ww's own `.ww` state, and `ww-rule-automation.json` are left
out. Without git there is no
change set: the globs select every file in the directory, `.git` and `.ww`
excepted.

In a shell check a bare `$WW_STEP_CHANGED_FILES` splits on whitespace, which
suits `grep -L foo $WW_STEP_CHANGED_FILES` and `xargs`; paths with spaces need
`printf '%s\n' "$WW_STEP_CHANGED_FILES" | xargs -d '\n' …`. A check's full
output is a command-output artifact: `ww artifacts` lists it under the step
with a `check` field naming the check.

### In the artifact

The step's artifact gains a `## Rules` section after `## Result`: one line per
rule with its status, `passed`, `not applicable`, `failed`, which only a
waiver lets through, `verified pass (by <verification item>)` for a rule a
verifier judged, or `passed (check <name>)` for one a derived check covers;
hook checks carry `(hook)`. A rule is `self-declared` only when the operator
waived it, which skips its verification too. The section ends with the
waived IDs and the operator's reason for each, if any, and how many
completions ww rejected before this one.

### How a rule becomes a check

A rule without a command is judged by another agent, and the first time its
wording is seen, that agent may propose a command for it; with the operator's
approval ww runs the command from then on, for every step and task that has
the rule. Nothing is reasoned about twice.

When a step's worker completes and its checks pass, ww holds the completion:
nothing is recorded, the artifact is kept as a draft, and **verification
items** are inserted before the step, one per distinct worker the rules ask
for through `agent`, `model`, and `reasoning` (else the step's). Each is its
own assignment: under `auto` the manager hands it to a new worker; under
`single` the same session performs it, and its page says to read the change
as a reviewer would. The verification page lists each rule and what is
asked about it, the step's changed files and the `git diff` that shows them,
the path of the held artifact, and the checks the project already has.

What ww knows lives in `ww-rule-automation.json` at the project root, a file
to commit, keyed by each rule's text hash; `checks` there are named, and one
check may cover many rules, the usual case for an ecosystem tool such as
deptrac, PHPStan, import-linter, ruff, or eslint, whose one configuration
expresses several rules. Scriptizing takes two stages, each ending at an
operator stop, `operator_reason: check_proposed`:

1. **Approach.** For an unknown rule the verifier writes an interpretation
   and a one-line approach: the tool or command, and the check it would
   create or extend. It builds nothing. It may also report the rule
   `not-convertible`, with a verdict, or `ambiguous`, with readings.
2. **Prepare.** For an approved approach the verifier builds the check:
   installs the tool as a development dependency, writes its configuration in
   the repository, proves the command fails on a deliberate violation and
   passes on the change, and reports the command. ww records it as
   `proposed`; the operator's approval converts it and its rules. An extended
   check keeps running as approved until its revision is approved.

For a rule that is not convertible, or was rejected, the verifier gives a
verdict, `pass` or `fail` with `file:line — what` evidence. A failing verdict
is a rejected completion: the step goes back to its worker with the fix page,
and it counts toward the rule's `max_fixes` like a failed check.

At the stop the operator decides each proposal with `next`: `--approve
<hash or check>`, `--approach <hash> "<text>"` to replace the verifier's
approach, `--pick <hash>=<number>` for an ambiguous rule, or `--force
--force-reason` to reject everything undecided, after which those rules are
judged. The CLI prints an approved command in full and asks before
recording it: there is no allowlist of executables, the operator's reading is
the safety. Once nothing is undecided, ww runs the step's checks again,
including a newly approved one, which may send the step back, then records
the held completion as submitted. The cost is two operator stops per rule
wording, once ever, batched per step. `ww lint` lists store entries whose
wording no rule has any more and entries awaiting a decision; only
`ww rules prune`, after listing them and asking the operator, deletes the
orphans.

When the last verifier's completion records the held completion under
`auto`, that verifier's assignment ends there: the step's automatic follow-ups
run, and its next agent item, such as an agent-owned `after_complete` hook,
waits for the manager to dispatch as a new assignment.

### Checking early and disputing a check

A worker need not complete to learn what the checks say: `ww check <task>`
runs the step's checks against what it changed so far and prints the
failures as the fix page would, or `All checks pass`, plus the rules a
verifier will judge once it completes. Nothing is recorded: no attempt
counts, and the output is not kept. It exits 1 when a check fails.

A check can be wrong for a change. After a rejection, instead of bending its
work around the check, the worker may dispute it with
`ww dispute <task> --rule <id> --reason "<why>"`, naming the ID the fix page
shows. The task stops with `operator_reason: check_disputed`; the page shows
the check, its last output, and the worker's argument. The operator decides:

- `next --retry`: the check stands. The rejection still counts toward
  `max_fixes`, and the step goes back to its worker with the fix page.
- `next --force --force-reason "<why>"`: the check is waived for this step
  only; the worker completes again without it, and the artifact records the
  waiver.

A dispute changes nothing in the rule-automation store. Every dispute is also
kept in `.ww/rule-disputes.json`, and `ww lint` lists each disputed ID with
how often and where it was last disputed, since a check disputed again and
again deserves a look at its wording or command.

### Reading the rules

`ww rule <task> <id>` prints one rule or check of a task as the task's plan
froze it: its full text, globs, rule file, command, and the steps that carry
it, and for a rule without a command what the store knows about its wording.
`ww rules` lists the project's declared groups, with their filters and each
rule's ID and first sentence, and each step's own rules; `--json` gives the
same for a program. `ww rules prune` deletes store entries no declared rule
needs any more, after listing them and asking; `--yes` skips the question.

### Writing rules with the ww-rule skill

Rules are easiest to add in conversation. Invoked as `/ww-rule`, or when you
ask an agent to add or change a rule, the `ww-rule` skill carries the
judgment: it reads the groups (`ww rules --json`) and the real workflow and
step names (`ww discover`), splits what you said into atomic obligations,
tells a new rule from an amendment of an existing one or a change of where a
group applies, gives a rule globs only when it names a kind of file and
counts what each matches, places it in a group whose filters fit, and
rewrites it as one imperative sentence with the rationale below. It shows
you one confirmation block, with each rule's ID, group, filters, globs and
match counts, sentence, and whether it is new or replaces an existing one,
and writes nothing before you answer. `/ww-rule split <file>` does the same
for every item of a prose document, in one batch; `/ww-rule from-review`
turns the lasting conventions in a task's last review or fix page into
rules.

The skill writes only through `ww rules` commands, which validate every
write: each loads the configuration as ww would once the write is made, and
when that fails, or the write would not have its effect, every file is put
back and the command reports why. `--dry-run` runs the same validation and
writes nothing. Nothing is committed.

```console
$ ww rules add --group php --dir rules/php --workflows task --steps develop
Added rule group `php` (rules/php/) to ww-rules.yaml.
Added ww-rules.yaml to imports in ww-agentic-workflows.yaml.
Warning: rules/php holds no rule yet, and git does not keep an empty directory: add one with `rules add php --text ...` before committing.
Reaches these steps (an agent step's page shows it):
- task: develop
$ ww rules add php --text "Put every \`*Service.php\` under \`src/Service/\`." --paths "src/**/*.php"
Created rules/php/put-every-service-php-under.md: rule `php/put-every-service-php-under`.
`src/**/*.php` matches 14 file(s) now.
Reaches these steps (an agent step's page shows it):
- task: develop
```

- `rules add <group> --text "<sentence and body>"` creates a rule file in the
  group's first directory, named after the first five words of its first
  sentence unless `--id` names it, with `--paths` globs and a check from
  `--check-shell` or `--check-argv` and `--assert empty|eq:<value>`. It never
  overwrites a file.
- `rules add --group <name> --dir <path>` adds a root group, with optional
  `--workflows` and `--steps` filters. ww never rewrites
  `ww-agentic-workflows.yaml`: the group goes into `ww-rules.yaml` next to
  it, a file ww owns and rewrites whole, and the repo file gains one entry
  under `imports` the first time, checked to change nothing else.
- `rules edit <id> --text ... --paths ...` replaces a rule file's body or
  globs and keeps every other line. A new wording has a new hash, so the
  command says which rule-automation store entry stops matching and, when
  the old wording had an approved check, that `rules promote` keeps it.
- `rules move <id> <group>` moves the file, unchanged, into another group's
  directory; its wording, and so what the store knows about it, stays.
- `rules filter <group> --workflows ... --steps ...` changes where a group of
  `ww-rules.yaml` applies (`--workflows '*'` writes `"*"`; `--all-workflows`
  and `--all-steps` remove a filter, which also means all); a group declared elsewhere is yours, and the command says what to
  write there.
- `rules promote <check>` copies an approved store check into the `check`
  frontmatter of every rule file it covers and removes the check and those
  rules' entries from the store, since a rule with its own command is never
  looked up there. It refuses a check awaiting a decision and a rule written
  in a step's own `rules` list.

Each command ends with the steps the rule or group now reaches. `ww rules`
names the approved store check of a rule without a command of its own
(`store_check` in `--json`), which is what a promotion would copy.

In the `auto` runtime a worker that keeps asking for its page after its
assignment ended is not given the manager's own work: its assignment token no
longer opens anything (see [Assignment tokens](#assignment-tokens)), and for a
`role: manager` or interactive step `instruction --role worker` names the step
as the manager's and offers no completion command.

### Rules from extensions

An extension may ship rule groups through its `rules`, each a
`RuleGroupContribution` naming the group, its absolute paths or other group
names, and optional filters. A project gets them by listing the extension in
the root `ww-agentic-workflows.json` `extensions`, even with an empty section;
a group name also declared in the YAML is an error.

`ww lint` ends with `Rules: N groups, M rules` when the configuration declares
any, after a notice for each absolute rule path.

## Hooks, variables, and transitions

### Hook scopes and ordering

Hooks share the same handler syntax. Global hooks can filter with `workflows`
and, for step lifecycle phases, `steps`; workflow hooks can filter step lifecycle
phases with `steps`; step hooks cannot use either filter. The workflow boundary
positions are `before_start_workflow` and `before_complete_workflow`. They run
once in global → workflow order, cannot use `steps`, and cannot be declared at
step scope. Step lifecycle positions are `before_in_progress`,
`before_complete`, and `after_complete`, and run in global → workflow → step
order for every matching step.
Step filters accept bare names at any nesting level or slash-separated logical
paths such as `plan-and-fix/fix` when only one substep should match.
Hooks and rule groups take one filter shape: `workflows` and `steps` are each
`"*"` for all or a list of names (`workflows: [task, bugfix]`); a bare name is
not a list, and `"*"` cannot be mixed with names. One difference: `[]` on a
hook means all, like omission, while on a rule group it means the group applies
only where a step names it. The
[specification](specification.md#workflow-and-step-filters) has the table.

A singular hook is written directly as the normal handler shape; there is no
`handler` wrapper. A name-only mapping references a catalog handler, while
action keys define an inline handler. Use `handlers` only to run multiple
ordered handlers under shared `workflows` and `steps` filters. Each grouped
entry uses that same handler shape, including the named-entry shorthand, and may
carry its own action selection. Put `workflow: "{{workflow}}"` directly on a
hook to declare a transition.

### Variables and metadata

Interpolations use `{{name}}`. Reserved built-ins are `{{__task_id}}` and
`{{__workflows}}`; the latter is the ordered workflow-name list, joined by commas.
The core `{{__task_workspace_dir}}` variable is always the canonical directory for
the task: the project root by default, the configured project directory when a
task was started with `--project`, or the selected task checkout when an
extension such as `ww/git` supplies one. A step or handler with a
[`workdir`](#choosing-where-a-step-works) other than `task` reads the directory
it chose instead. `{{__project}}` is that project's name
and `{{__project_dir}}` its directory, both empty for a task in the root;
`{{__project_dir}}` keeps pointing at the project even after a worktree moves
the task workspace. `{{__projects}}` lists every configured project name, joined
by commas.
Values in a handler’s `provide` list are declared inputs for that handler. Each
entry supports either `name` plus an optional `description`, or the same compact
`name: description` shorthand as handlers and steps. A step’s values are
available to its completion hooks and later plan items.

```yaml
provide:
  - workflow: The workflow name corresponding to one of {{__workflows}}.
```

Use `update_metadata` on an agent-owned handler or step to retain values. Each
entry gives the agent-facing completion name, its dotted storage path, an
optional instruction, and an optional `scope`. The default `task` scope retains
values across workflow runs of one task:

```yaml
steps:
  - name: create-jira
    mcp: jira
    description: Create the issue and retain its ID.
    update_metadata:
      - jira_id: The ID of the created Jira issue.
        key: integrations.jira.issue_id
  - name: inspect-jira
    description: Inspect {{metadata.integrations.jira.issue_id}}.
```

The completion instruction includes every required value as a named argument:

```console
ww-agentic-workflows complete TASK-123 --role worker \
  --metadata jira_id="PROJ-456" --artifact="<result>"
```

Metadata is task-scoped rather than workflow-scoped. `ww` stores it as a nested
object under `metadata` in `.ww/tasks/<task-id>/metadata.json`; a later run can use the
same `{{metadata.<path>}}` reference. Metadata leaves are strings. A new value
may replace the same path, while a leaf/object path collision is rejected.
Inspect the complete object as JSON with `ww-agentic-workflows metadata TASK-123`.

A leaf declared with `append: true` is a list that grows across completions and
runs. Each completion passes the name once per value, or not at all, and ww
appends the values to what is stored, dropping repeats, without touching other
keys. The leaf interpolates as a comma-separated list, and as an empty string
before anything was saved, so a prompt never shows a raw placeholder. This is
how one task remembers the pull request threads it already handled across
several review passes:

```yaml
- process_pull_request: Create one work item per unresolved reviewer thread.
  items:
    report_item: Reply in the thread and resolve it.
    update_metadata:
      - handled_comments: The root comment id of the thread you just resolved.
        key: pull_request.handled_comments
        append: true
- get_pull_request_comments: >-
    Threads whose root comment id is in {{metadata.pull_request.handled_comments}}
    are already handled; list them as needing no work.
```

```console
ww-agentic-workflows complete TASK-123 --role worker \
  --metadata handled_comments=4711 --metadata handled_comments=4718 \
  --artifact="<result>" --summary-for-next-step="<handover>"
```

Under `items`, `update_metadata` belongs to the built-in `handle-item` stage; with
configured stages, declare it on the stage that produces the value.

Use `scope: project` for values shared by every task in the project. Project
metadata has an explicit interpolation namespace so the ownership of a value is
visible where it is consumed:

```yaml
steps:
  - name: discover-environment
    description: Determine the shared staging URL.
    update_metadata:
      - staging_url: The staging environment URL.
        key: environments.staging.url
        scope: project
  - name: deploy
    description: Deploy to {{project_metadata.environments.staging.url}}.
```

The completion command still uses `--metadata`; the saved plan determines the
declared destination. Project metadata is stored as nested JSON in
`.ww/metadata.json` and can be inspected with
`ww-agentic-workflows metadata --project`. Project metadata is resolved live;
copy a value into task metadata when a task needs a stable snapshot. Metadata
is plain runtime state and should not be used for secrets.

Every workflow also receives ww's built-in `update-workflow-summary` handler as
its final `before_complete_workflow` action. It asks the agent for a concise
goal/result summary, and ww writes that value to the task run ledger when the
run completes. No `../ww-agentic-workflows.yaml` configuration is needed. Its instruction
lists every ordinary step's handover of this run in order, each with its
artifact, and tells the agent to build the summary from those alone, so the
summary cannot borrow counts or statuses from other runs or stale material. It
requests the run's ordinary worker selection (`auto`), unlike `init`, which
requests `cheapest` / `low`; `builtins.workflow_summary` in
`../ww-agentic-workflows.json` overrides that.

## Interactive steps

Some work is a conversation with the person operating ww: agreeing an
architecture, or performing a manual test case and reporting what happened.
Mark such a step `interactive: true`:

```yaml
- think_about_architecture: >-
    Set up the architecture. Discuss it with the operator: listen, ask
    questions, clarify edge cases.
  interactive: true
```

The operator runs no `ww` command. They talk to the agent, and the agent reads
from their words whether the conversation continues or is finished. What ww
adds is the record and the gate. The step's page carries the contract and the
commands; the agent records both sides as the conversation goes, verbatim:

```console
ww-agentic-workflows interact TASK-123 --role manager --operator "Resize through the queue; upload never waits."
ww-agentic-workflows interact TASK-123 --role manager --agent "Proposed: a listener dispatches a queued job per upload."
ww-agentic-workflows interact TASK-123 --role manager --end-interaction
```

Every entry is appended to one file per task,
`.ww/tasks/<task-id>/interactions.md`, never rewritten, each headed by the time,
run, step, the item when the step is a per-item stage, and speaker, so the file
reads as the task's whole history of operator involvement;
`ww-agentic-workflows interactions TASK-123` prints it. The step's page shows
the conversation recorded for it so far.
Completing an interactive step is refused until at least one entry was recorded
and the interaction was ended. The step's artifact and handover are written as
usual; what goes into them is the agent's judgement.

When the operator's answer is one of a few outcomes, declare them as `choices`
and ww turns them into a real pick rather than free text:

```yaml
items:
  process_item: Show the test case to the operator.
  interactive: true
  choices:
    - pass: The test case passed.
    - fail: The test case failed; no comment.
    - fail and give comment: The test case failed; the operator explains why.
    - skip: Skip this test case.
```

The page lists the choices in order and tells the agent how to offer them for
its integration, with the question tool each agent offers: `AskUserQuestion`
in Claude Code, `request_user_input` in Codex, `ask_user` in Gemini CLI,
`AskQuestion` in Cursor, `ask_question` in Antigravity, and
`ask_user_question` in Grok CLI, where the operator picks with the keyboard.
Several agents offer the tool only in some modes, Codex in Plan mode for
example, so the page also says to fall back to a numbered list when the tool
is not available; Kimi, DeepSeek, and custom agents get the numbered list
straight away. The tool names come from the
[askmux](https://github.com/iShaldam/askmux) question-tool matrix (MIT,
Copyright (c) 2026 iShaldam) and the Gemini CLI documentation. The pick is recorded with
`interact --choice "<label or number>"`, a comment the operator adds goes in as
`--operator` text, and ending the interaction is refused until a choice was
recorded. The chosen label is kept on the step record and in the interactions
file.

The limitation to know: a delegated worker is a subagent and cannot talk to the
operator. An interactive step is therefore always performed by the session that
holds the conversation, the manager in the `auto` runtime: it is a
`role: manager` step, and the step's profile, agent, model, and reasoning
are ignored. For a manual-testing workflow, put `interactive: true` on the
per-item stage: the manager presents each test case, waits for the operator's
result, records it, ends the interaction, and completes the item.

### The operator page

A per-item stage declared with `ui: true` is answered in the browser, on an
answer sheet over every item of the run, and ww applies the answers itself.
The key requires `interactive: true` and is valid on one per-item stage per
`items` step, because the page is shaped for items: other structures would
need a page of their own, and none exists yet.

```yaml
items:
  process_item: Show the test case to the operator.
  interactive: true
  ui: true
  choices:
    - pass: The test case passed.
    - fail: The test case failed; the operator explains why.
```

The page is an extra on top of the engine. The core knows it by the `ui`
flag alone: the stage's page tells the agent to run one command, and
everything else lives in the `operator_ui` package, which drives the task
only through the public commands an agent uses.

```console
ww-agentic-workflows interact TASK-123 --role manager --await
```

The command applies any answers left over from an earlier wait, serves the
page on `127.0.0.1` for as long as it runs, opens the browser unless a tab is
already polling, waits, and then applies what was answered. It returns when
every item is answered, when the operator presses "I'm done for now", when
the operator closes the page, or after `WW_OPERATOR_WAIT` seconds with
nothing new. The port is derived from the task ID, so the tab survives
between waits and reconnects by itself; `WW_OPERATOR_PORT` fixes it. There
is no daemon. After the step's page, the command prints how the wait ended,
which items it applied, how many are answered, and whether to wait again or
stop.

How the agent runs the wait depends on what its shell can do, and the step's
page says which, from the same kind of per-agent table as the choice
mechanisms. Claude Code has a background shell, so the page tells it to run
the command with `run_in_background` and go on with the conversation: the
operator can talk to the agent in the session while the page is open, and
the command's output reaches the agent when the wait ends. Such a wait may
be long, so the page puts `WW_OPERATOR_WAIT=1800` in front of the command.
While it runs, the agent must not run commands that change the task, because
the wait applies the answers the moment it ends; `status` and `instruction`
are fine. Any other agent blocks on the command, so the default wait is `90`
seconds, under its shell timeout, and the agent runs it again while items
remain.

The page lists every item with its text and analysis. The operator answers
the items in any order, each with the stage's choices when it declares them
and a comment, and can revise an answer until ww has applied it. An answer
is recorded the moment it is given, as one atomic replacement of the answer
sheet, `.ww/operator-ui/<task-id>.json`, under the task lock and before the
browser is told it went through. It does not touch the task's state. A
failed submission comes back to the page as an error next to the item and
the draft stays; an answer given while no agent is waiting is kept in the
tab and sent when a wait is back, and is lost if the tab is closed first.
Drafts survive a reload. The sheet remembers when its task was created, so a
sheet left behind by a reset task is discarded rather than applied to the
task that reuses the ID.

Applying walks the plan in order from the current stage. For every `ui`
stage whose item has an answer on the sheet, ww records the pick and the
comment and ends the interaction with `interact`, writes the answer onto
the item with `update-item` as its `actual_solution` (the pick, then the
comment after a colon) and marks it resolved for a `handle-item` or
`resolve_item` stage and reported for a `handle-item` or `report_item`
stage, completes the stage with `complete` and an artifact written from the
answer, and only then takes the answer off the sheet, so a wait that is
killed mid-way leaves at most a stale entry that the next wait drops. A
document the stage promised to update is the one thing ww cannot write: the
printed result names such documents and tells the agent to record the
answers of the applied items in them first. When the operator closes the
page, the result tells the agent not to open it again on its own. The walk stops at the first stage whose item has no answer, at any
stage that is not a `ui` stage, and wherever ww needs the agent. Items are
therefore completed one by one in plan order whatever order the operator
answered in; an answer for a later item waits on the sheet. The applied
answer is what an agent would have recorded: the stage's chosen label, the
comment in the interactions file, and the artifact. The agent's loop is:
wait, read the printed result, wait again while items remain.

A pause is the operator saying they are done for now, from the page or
recorded by the agent with `interact --pause`. It is kept on the task, and
the page's pause is recorded after the answers given before it were applied.
The step's page then tells the agent to stop, without waiting again, and to
show the step with `instruction` when the operator returns. Only the
operator's own words lift a pause: an answer on the page or an `--operator`
or `--choice` entry, not the agent's words and not the ending of a step.

## Documents

Metadata holds small values that steer a workflow. A document is the durable,
free-format counterpart: a file a workflow builds up and returns to across
runs, such as the test cases derived from an issue that a second run refines
after the issue changed and a `build-test-report` workflow reads later.
Declare documents once at the root; a task-scoped document lives under the
task directory and a `scope: project` one under `.ww/documents`, unless `path`
places it elsewhere in the project, for example
`documentation/issues/{task_id}/notes.md`, which then resolves inside the
task's worktree when the run has one:

```yaml
documents:
  - test_cases: The test cases derived from the issue, kept current across runs.
workflows:
  - name: derive-tests
    steps:
      - derive: Analyze the issue and derive test cases.
        update_document:
          - test_cases: One checklist item per case; keep items that still hold.
  - name: build-test-report
    steps:
      - report: Build the report from {{documents.test_cases}}.
```

A step that declares `update_document` sees a "Documents to update" section
naming each document, its absolute path, whether it exists yet, and the
instruction. Its worker edits the file in place, the one exception to the rule
against writing under `.ww`. On completion ww checks that each promised file
exists and journals who updated it, run, step, time, and content hash, in the
task's `documents.json` or the project's. `{{documents.<name>}}` resolves to the
absolute path for the current filesystem, like the worktree path does, so a
task started on the host reads correctly inside a container. `reset` removes a
task's journal and the documents kept under its `.ww` directory, as it removes
the task's metadata; a document declared with a `path` is the workflow's own
file and stays in place.

`ww-agentic-workflows documents TASK-123` lists every declared document with
its scope, path, whether it exists, and its last update; without a task ID it
lists the project-scoped ones.

## Artifacts and dependencies

Agent-owned steps save their full Markdown result as an artifact by default.
Use `artifact: false` for work that has no durable output. A loop wrapper also
saves the final result supplied by its successful stop command as its main
artifact; `artifact: false` on the wrapper disables that file independently of
its body-step artifacts. `depends_on` names an earlier artifact-producing step
and adds that artifact to the later step's instruction; execution still
follows the written step order.

A nested step, in a group, a loop body, an assessment outcome, or a per-item
stage, may also name an earlier step of any enclosing level. The nearest match
wins: an earlier sibling first, then an earlier step of the parent's level,
and so on up to the workflow's top-level steps and `init`. An outcome may name
its assessment and a per-item stage its `items` step, whose work has finished
by then; a loop body cannot name its own running loop, and no step can name a
plain group, which saves no artifact of its own. The instruction names the
dependency by its step path, such as `review/check`, which is how
`ww artifacts` lists it.

Filesystem artifact names begin with each step's one-based declaration ordinal
among its siblings. Nested containers reset the ordinal for their children, so
a workflow with `init`, `investigate`, and a `plan-and-fix` group is stored as:

```text
steps/
  01-init.md
  02-investigate.md
  03-plan-and-fix/
    01-plan.md
    02-fix.md
```

Loop bodies add an `iteration-NN` directory beneath each loop wrapper. Every
round therefore keeps its own step and hook artifacts instead of overwriting
the files from an earlier round:

```text
steps/
  02-review-and-fix.md
  02-review-and-fix/
    iteration-01/
      01-review.md
      02-fix.md
    iteration-02/
      01-review.md
```

These prefixes describe the workflow hierarchy, not the flat lifecycle-plan
position. Hook artifacts use their plan position within the target step's
`.hooks/<phase>/` directory.

```yaml
workflows:
  - name: task
    steps:
      - name: research
        description: Investigate the change and record the findings.
      - name: implement
        description: Implement the change.
        depends_on: research
      - name: review-and-fix
        loop:
          - name: fix
            description: Fix what the review found.
            depends_on: research
            break: Nothing is left to fix.
      - name: notify
        description: Report that implementation is complete.
        artifact: false
```

## Dynamic per-item workflows

One `items` step collects a runtime list of work items and then runs a fresh
lifecycle for every item. The step's own work is the collection: the agent
splits it into items with `add-item`. In the minimal form, every item then gets
one built-in stage that analyzes, resolves, and reports it:

```yaml
workflows:
  - name: handle-feedback
    steps:
      - review: Review the pull request.
        items: Split based on the comments retrieved from the Bitbucket pull request.
```

The string is splitting guidance shown to the collecting agent. Use `items: ~`
when no guidance is needed.

The built-in stage can take guidance for each of its phases without declaring
stages. Under `items`, `process_item`, `resolve_item`, and `report_item` are
strings appended to the `handle-item` prompt as "When analyzing it", "When
resolving it", and "When reporting the outcome"; the item is still handled in
one pass with one artifact:

```yaml
- process_pull_request: Split the work by comment and sub-comment.
  items:
    item_assignment: all_items
    report_item: Reply in the same Bitbucket comment thread, then resolve the thread.
```

To control the stages, list them under `items.steps`. Mark stages with
`process_item: ~`, `resolve_item: ~`, or `report_item: ~` when they update those
standard item fields. `steps: []` collects items without processing them, for
example when a later step reads them.

```yaml
workflows:
  - name: handle-feedback
    steps:
      - review: Review the pull request.
        items:
          description: Split by pull request comment.
          steps:
            - analyze: Analyze this comment.
              process_item: ~
            - fix: Resolve this comment.
              resolve_item: ~
              model: opus
            - reply: Report the outcome.
              report_item: ~
```

Worker settings cascade from the `items` step, to `items`, to each stage. The
step's `agent`, `model`, `reasoning`, `profile`, and `role` apply to
collection and are inherited by the stages; the same keys under `items` apply
only to the stages, and a stage's own value wins.

### One worker per item or for all items

In the `auto` runtime, `item_assignment` decides how many manager-dispatched
assignments the per-item stages become. By default one worker keeps going
through every stage of every item:

```yaml
- review: Review the change and record each finding as an item.
  items:
    item_assignment: per_item   # or all_items
    model: sonnet
    steps:
      - analyze: Analyze this finding.
        process_item: ~
      - fix: Resolve this finding.
        resolve_item: ~
      - reply: Report the outcome.
        report_item: ~
```

- `all_items`, the default, keeps one worker for every stage of every item.
- `per_item` keeps one worker for all stages of one item; the next item is a
  new assignment.
- `per_step` hands every stage back to the manager.

The manager's preview names the scope before dispatching. The first stage of
the assignment tells the worker which stages it covers and to do them one at a
time. Each later stage arrives as a short instruction with only the new stage's
work and its completion command, because the worker already has the role,
workspace, profile, and item context. Every stage is still completed, saved,
and recoverable on its own, so `next`, `status`, reloads, and interrupted-hook
recovery behave exactly as with separate assignments. Because one worker
performs the shared stages, a stage that requests a different agent, model,
reasoning, or profile, or is the manager's (`role: manager`), starts a new assignment,
exactly as a loop body step does. The setting has no effect in
the `single` runtime.

A workflow may contain at most one `items` step. This is intentional: collected
items belong to the workflow run, and ww expands every per-item stage in one
place when collection completes.

### Items shared across runs

Some item lists are the task's, not one run's: the test cases of a manual
test plan are the same in round one and round two. Declare the flow `shared`
and the items outlive the run:

```yaml
- collect: >-
    Read the test cases in docs/test-cases.md. Compare them with the stored
    items and make the items match: add missing cases, remove cases that
    are gone, reword cases that changed. Keep the item ID equal to the
    case's heading.
  items:
    shared: true
    interactive: true
    ui: true
    choices:
      - pass: The test case passed.
      - fail: The test case failed; the operator explains why.
```

ww keeps the task's canonical list in `.ww/tasks/<task-id>/items.json` and
refreshes it whenever a run adds, changes, or resolves an item, so it always
holds the latest outcome of every item. Each run still keeps its own copy in
its record, so round one's results stay readable after round two. A new run,
whether the task is started again or a handoff enters the workflow, starts
from the stored items with their outcome fields cleared: every round is a
fresh round over the same cases.

The collection step then reconciles instead of splitting. Its page lists
the stored items and carries the commands to make the list match the
source: `add-item` for new cases, `remove-item` for cases that are gone,
and `update-item --item` to reword one. Both are allowed only while the
collection step is in progress, and an item that other items refer to
cannot be removed. Stable IDs matter: an item that changed is reworded under
its ID, not replaced, so its history lines up across rounds. When nothing
changed, the agent completes the step as it is. With nothing stored yet, the
same page reads as a plain split.

```console
ww-agentic-workflows remove-item TASK-123 --id case-7
ww-agentic-workflows update-item TASK-123 --id case-3 --item "Upload a 25 MB image."
```

To start over from the source, `start --fresh-items` forgets the stored
items before the run begins, and the collection step splits anew. `reset`
removes the store with the task.

### Custom item fields

An item carries the standard analysis and outcome fields, and any custom
fields a workflow needs: the ID of the source comment, the ID of the reply
posted for it, a flag. Values are strings. They are set with the item
commands, several in one call, and read back with `item` or `items`:

```console
ww-agentic-workflows add-item TASK-123 --id c1 --item "Rename it." --field bitbucket_comment_id=100
ww-agentic-workflows update-item TASK-123 --id c1 --field bitbucket_reply_id=200 --field bitbucket_thread_resolved=true
ww-agentic-workflows item TASK-123 --by bitbucket_reply_id=200
```

A step declares the fields it sets with `update_item`, beside
`update_metadata` and `update_document`, and ww refuses to complete the step
while any of them is empty on the step's item, or on every item when the
step is the collection:

```yaml
- reply: Reply in the Bitbucket thread, then resolve it.
  report_item: ~
  update_item:
    - bitbucket_reply_id: The ID of the reply you posted.
    - bitbucket_thread_resolved: Set to true once the thread is resolved.
```

Per-item stage prompts can read the stage's own item: `{{item.id}}`,
`{{item.text}}`, and `{{field.<name>}}`.

Two flow-level rules make deduplication a refusal rather than a hope:

```yaml
items:
  shared: true
  identity: bitbucket_comment_id
  unique: [bitbucket_comment_id, bitbucket_reply_id]
```

`identity` names the field every new item must carry. `unique` is one pool
of values across the listed fields: a value may appear once over all items,
in the run and, when the flow is shared, in the task's stored items, so a
reply posted in round one cannot become an item in round two, and no two
items can claim the same comment. `add-item` and `update-item` refuse a
duplicate and name the item that holds it. The collection page states both
rules and the stored list shows each item's fields, so reconciling is a
diff on real IDs.

During collection and processing, use the item commands to maintain the
structured records:

```console
ww-agentic-workflows add-item TASK-123 --id comment-1 \
  --item="The error path is not tested."
ww-agentic-workflows items TASK-123
ww-agentic-workflows item TASK-123 --id comment-1
ww-agentic-workflows update-item TASK-123 --id comment-1 \
  --processed-item="Missing failure-path coverage" \
  --proposed-solution="Add an integration test"
ww-agentic-workflows update-item TASK-123 --id comment-1 \
  --actual-solution="Added the integration test" --resolved=true --reported=true
```

`update-item` confirms the saved item and returns the current worker completion
command.

## Worker-controlled loop control

A step can wrap an ordered `loop` body. Individual agent-owned body steps may
declare natural-language `break` or `continue` conditions, so review and remediation stay
visible as separate assignments and more than one worker may be authorized to
end the loop. Body steps retain hooks, profiles, artifacts, nesting, and item
collection.

```yaml
workflows:
  - task: ~
    steps:
      - review-and-fix: ~
        loop_max_times: 5
        loop:
          - review: Review the implementation and report meaningful findings.
            break: The review has no meaningful findings.
          - fix: Fix the reported findings.
```

After doing a break-enabled step, the worker evaluates its break gate. When the
condition holds, it uses the displayed command instead of ordinary completion;
the command accepts the same artifact, variable, metadata, and execution fields
as `complete`:

```console
ww-agentic-workflows loop TASK-123 --break --role worker \
  --artifact="<whole result in Markdown>"
```

The breaking step's completion hooks still run before ww skips the remaining
body. A step may instead declare `continue`; its worker command runs completion
hooks and then skips the rest of the current body, restarting from the first
body step:

```console
ww-agentic-workflows loop TASK-123 --continue --role worker \
  --artifact="<whole result in Markdown>"
```

If no worker breaks or continues, the body repeats automatically. Each repetition gives
automatic actions fresh operation IDs, while retries within one iteration
retain their existing idempotency identity.

### One worker per loop round

In the `auto` runtime, `loop_assignment` decides how the body of one round is
split into worker assignments, like `item_assignment` does for per-item
stages:

- `per_iteration`, the default, keeps consecutive body steps in one assignment
  while they resolve to the same agent, model, reasoning, and profile. The
  worker completes each step with its own command and receives the next step
  straight away. The repeat boundary always ends the assignment, so the
  manager still sees every round and still receives the limit escalation.
- `per_step` hands every body step back to the manager.

Body steps keep their own `profile`, `agent`, `model`, and `reasoning`
overrides under `per_iteration`; a step whose settings differ from the
worker's simply starts a new assignment. That is deliberate: a `code-reviewer`
review followed by a `developer` fix stays two workers, so the reviewer never
fixes its own findings, while a fix-until-green loop under one profile runs as
one worker per round.

```yaml
- run-tests: ~
  loop_assignment: per_iteration
  profile: quick-developer
  loop:
    - test: Run the test suite.
      break: No failures found.
    - fix-tests: Fix the failures.
```

Every body step's work instruction names the loop and the round. The first
round is described as building on the work of the steps before the loop; a
later round tells the worker to concentrate on the previous rounds of this
loop, not on the whole task, and shows the `artifacts` command that lists the
earlier iteration directories. The setting has no effect in the `single`
runtime.

ww limits loops to three iterations by default. Set a different project-wide
positive integer in `../ww-agentic-workflows.json`, or override one wrapper in
`../ww-agentic-workflows.yaml`:

```json
{"loop_max_times": 4, "extensions": {}}
```

```yaml
- review-and-fix: ~
  loop_max_times: 7
  loop:
    - review: Review the implementation.
    - fix: Fix the findings.
```

The effective limit is saved in the compiled plan. When the completed iteration
count reaches it, ww does not begin another round or provide a continuation
command. The response reports `awaiting_operator` with `operator_reason:
loop_limit` and explicitly tells the manager to escalate the saved results and warning to the user for manual
resolution. It also shows the operator's one way past the limit: once the user
has resolved or accepted the remaining findings, `next --force --force-reason`
leaves the loop, records the reason on the repeat boundary, and continues with
the steps after the loop wrapper. Nothing else starts another iteration; a
further round needs a higher `loop_max_times`.

## Parent and child tasks

Use `children: ~` to collect a parent task's child descriptions and
`workflow_per_child` to run one selected workflow for each child. Child tasks
live under the parent task directory and are intentionally limited to one level
for now.

```yaml
workflows:
  - name: feature
    steps:
      - name: split-work
        children: ~
      - name: implement-children
        workflow_per_child: implementation

  - name: implementation
    steps:
      - name: implement
        description: Implement this child task.
```

While the collection step is active, record each child:

```console
ww-agentic-workflows add-child TASK-123 --description "Implement the API"
```

Without `--id`, ww uses the configured `task_format`, the child's project's
own when `--project` names one that sets it, else the root's, or the usual
generated `TASK-<timestamp>` ID when no format is configured. `{timestamp}`,
`{digit}`, and `{uuid}` are the supported placeholders. Supply `--id TASK-123.1` when you
want a stable, human-chosen child label instead. When the child workflow's first
step provides `task_id`, omit `--id` and the child obtains its own ID from that
step; see [children that bind their own IDs](#children-that-bind-their-own-ids).
`--project <name>` runs the child in a configured project directory.

After the parent reaches its coordinator step, start a chosen child:

```console
ww-agentic-workflows child start TASK-123 TASK-123.1
```

The child runs as `TASK-123/TASK-123.1`, with its own hooks, items, and run
history. The parent waits while a child is active. Completing the final child
automatically resumes and completes the parent's normal lifecycle; parent
`after_complete` hooks still run.

Steps may contain recursive `steps` without a depth limit. The plan remains
flat: each leaf action uses a hierarchical ID such as `parent/child`, with its
immediate `parent` recorded in JSON and shown in Markdown. Parent steps are
stateful grouping boundaries: they become in-progress with their first child and
complete when every child and parent completion hook completes.

Use `handoff: true` on a workflow such as `decide-on-workflow` when its terminal
workflow transition starts a successor and never returns. The transition is a
step with `workflow` beside its name, usually interpolating a value an earlier
step provided; the same key on an `after_complete` hook of the last step is
equivalent:

```yaml
- name: decide-on-workflow
  handoff: true
  steps:
    - classify: Decide which workflow fits this request.
      artifact: false
      provide:
        - workflow: One of {{__workflows}}, other than decide-on-workflow.
    - route: ~
      workflow: "{{workflow}}"
```

Loading the configuration rejects a transition anywhere but last, more than one
transition, or a transition in a workflow without `handoff: true`. The plan
marks the workflow as a handoff. Execution
completes the selection workflow's run, records
`<original-workflow>=<next-workflow>` in authoritative task state, and starts the
successor as the task's next numbered run — so `ww instruction <task>` lists both,
each with its own plan, state and artifacts.

Handoffs are deliberately limited, and the limits are accepted for now:

- A workflow has exactly one transition, and it is the last thing the workflow
  does; there is no routing to different workflows from different steps. Choose
  the target dynamically with `{{workflow}}` instead, or branch earlier with an
  assessment.
- A task hands off once. The successor cannot hand off again; a second
  transition fails with "already has a handoff marker".
- A workflow cannot call another workflow and continue afterwards. Use child
  tasks to run another workflow per piece of work, or a reusable `handler` step
  tree to share a phase between workflows.

General nested workflow definitions remain deferred and are rejected when
`../ww-agentic-workflows.yaml` is loaded.

## Extensions

An extension adds handlers, modes, and commands to ww. It is identified as
`vendor/name` and is always referenced by its full address, never by a bare
name:

```yaml
workflows:
  - name: task
    modes:
      - ext/ww/git/modes:conventional-commits
    steps:
      - name: work
        prompt: true
        hooks:
          before_in_progress:
            - name: ext/ww/git/handlers:is-git-clean
          after_complete:
            - name: ext/ww/git/handlers:git-commit
```

Because every reference is qualified, installing an extension can never change
what a name already means in your project: nothing it provides takes effect
until you name it. Extensions do not provide hooks — a hook decides *when* work
runs against a particular workflow and step, which is your configuration's
business, not the extension's.

An extension handler runs inside ww rather than as a shell command, so it can
remember what it did. `ww/git`'s `git-commit` commits and then records the
commit, and `commits` reads that back. It stages the task workspace first; when
nothing is staged, for example after a review round that needed no fix, it
succeeds without a commit and without invoking project pre-commit hooks such as
Husky or lint-staged:

```console
ww-agentic-workflows extensions                          # what is installed, and what it provides
ww-agentic-workflows extension ww/git commits            # every commit ww made
ww-agentic-workflows extension ww/git commits TASK-123   # just this task's
```

With Git worktrees enabled, branch creation and worktree selection are separate
handlers. The default project workflow runs them together, but you may move
`create-worktree` to a later hook when you want to use the primary checkout
instead:

```yaml
hooks:
  before_start_workflow:
    - name: ext/ww/git/handlers:start-task-branch
    - name: ext/ww/git/handlers:create-worktree
```

`start-task-branch` only creates or adopts the branch. `create-worktree` is a
no-op when worktrees are disabled. If the primary checkout is already on the
exact task branch rendered from the workflow's configured branch name format,
either handler selects it as the task workspace rather than creating a separate
worktree. No conventional branch names such as a base or development branch
participate in that decision.

Each extension owns `.ww/ext/<vendor>/<name>/`, reached only through the store
object it is handed, and every write goes through ww's lock layer. Use
`store.update_text(name, callback)` when new content depends on existing content;
it holds one lock across the complete read–modify–write sequence.

### Configuring one

Extension settings live in `../ww-agentic-workflows.json`, ww's project config file —
separate from `../ww-agentic-workflows.yaml`, which describes what a workflow *does*:

```json
{
  "extensions": {
    "ww/git": {
      "commit_message": "{{task_id}}: {{commit_message}}",
      "base_branches": {
        "default": "main",
        "bugfix": "develop",
        "task": {"argv": ["./scripts/base-branch", "{{workflow}}"]}
      },
      "use_separate_branch": true,
      "branch_name_formats": {
        "default": "feature/{{task_id}}",
        "bugfix": "hotfix/{{task_id}}"
      },
      "worktrees": false
    }
  }
}
```

`"ww/git"` is the canonical key; a bare `"git"` also works when only one
installed extension has that name. An extension receives its own section and
nothing else from the file, and validates it itself — ww cannot know a third
party's schema. A section naming no installed extension is an error rather than
ignored, because a block that silently applies to nothing looks configured and
is not.

For `ww/git`, `ww-agentic-workflows extension ww/git settings` prints what actually resolved,
which is the first thing to run after editing the file; `--project <name>`
prints what a task in that configured project receives.

When `worktrees` is enabled, generated task IDs reserve any existing path
rendered by `worktree_dir` and `worktree_name_format`. For example, an existing
`worktrees/TASK-1` makes the next `{digit}`-formatted task use `TASK-2`, rather
than adopting that checkout for a new task.

`base_branches` maps exact workflow names to base branches, and its `default`
entry covers every other workflow, the same shape as `branch_name_formats`. A
repository under a configured project with a base branch of its own sets
`base_branches` in [its own settings file](#a-projects-own-extension-settings).
Each value may be either a literal branch name or an
object with a non-empty `argv` array. A top-level `base_branch` is refused
with a message pointing at `base_branches.default`, which replaced it. An argv command runs directly without a shell in
the project root; its single non-empty stdout line becomes the base branch.
Arguments may interpolate `{{task_id}}`, `{{workflow}}`, and `{{run_id}}`.
The resolved base is recorded with the task branch so retries, worktree creation,
and return-to-base use one stable value. A child task always uses its recorded
parent task branch instead.

`branch_name_formats` names the available branch naming strategies. Without an
override, `start-task-branch` first looks for a strategy matching the workflow
name and then falls back to `default`. Select another configured strategy for a
single run by name:

```console
ww-agentic-workflows start TASK-123 --workflow task --agent codex \
  --init-artifact="Fix the requested bug." --branch-strategy bugfix
```

The selection is persisted with the run, so later or retried branch and
worktree handlers use the same format. An unknown explicit strategy fails
instead of silently falling back.

`on_signing_failure` says what `git-commit` does when git cannot sign a
commit, for example because the signing agent is locked while the run goes on
unattended. `operator`, the default, stops for the operator as for any failed
handler. `unsigned` commits once more with `commit.gpgsign=false`, records
`signed: false` for the commit, and says so in the handler's result; any
other commit error still stops. Set it where the unattended run happens, such
as the machine or local settings file:

```json
{ "extensions": { "ww/git": { "on_signing_failure": "unsigned" } } }
```

#### What `ww/git` does with those settings

| Handler | Settings it acts on |
| --- | --- |
| `git-commit` | `commit_format`, `on_signing_failure` |
| `start-task-branch` | `base_branches`, `use_separate_branch`, `branch_name_formats`, `worktrees`, `worktree_dir`, `worktree_name_format` |
| `return-to-base-branch` | `base_branches`, `use_separate_branch` |
| `remove-task-worktree` | `worktrees` |
| `is-git-clean` | — |

A suggested wiring, with `is-git-clean` before `start-task-branch` so a branch is
never cut from a dirty tree:

```yaml
hooks:
  before_start_workflow:
    - name: ext/ww/git/handlers:is-git-clean
    - name: ext/ww/git/handlers:start-task-branch
  before_complete_workflow:
    - handlers:
        - ext/ww/git/handlers:git-commit: ~
        - ext/ww/git/handlers:return-to-base-branch: ~
```

`remove-task-worktree` is left out on purpose: a worktree is where the work
happened, so deleting it the moment a workflow ends should be your choice, not a
default. `ww-agentic-workflows extension ww/git branches [TASK-ID]` shows what was opened, and
`commits` what was committed.

`create-worktree` reuses a worktree that already exists at the configured
path. When none does but git reports the task branch checked out in another
worktree, left by an earlier round, moved by hand, or created before the
settings changed, the handler adopts that worktree and records its location
rather than failing on git's one-worktree-per-branch rule.

When `create-worktree` selects a checkout, ww persists its path for the run.
Later shell and extension handlers — including `git-commit` — execute there,
and Markdown agent instructions include a `cd` command so manual work uses the
same checkout. JSON instructions expose the path as `working_directory`. ww
persists the path relative to the project root and prints it absolute for the
filesystem it runs in, so a task started on the host continues correctly inside
a container that mounts the checkout elsewhere; project-local profile files are
recorded and printed the same way.
`ww-agentic-workflows extension ww/git branches <TASK-ID>` also reports it.

### Writing one

Create `<project>/ext/<vendor>/<name>/extension.py` exposing `EXTENSION`, or
publish a package advertising an `ww.extensions` entry point:

```python
from ww.extensions.api import Extension, ExtensionHandler, ExtensionResult

def _greet(context):
    return ExtensionResult(
        True,
        f"hello from {context.root}",
        values={"greeting": "hello"},
    )

EXTENSION = Extension(
    vendor="acme",
    name="hello",
    version="1.0.0",
    description="A minimal example.",
    handlers=(
        ExtensionHandler(
            "greet", _greet, "Say hello.", outputs=("greeting",)
        ),
    ),
)
```

For a packaged extension, name the `ww.extensions` entry point `acme.hello`;
the dot maps to the `acme/hello` identifier without importing the package.
Discovery is lazy, so unrelated workflows and saved-state commands do not load
the extension. An extension is trusted in-process Python once referenced:
qualified references isolate names, not side effects or process access.

The context identifies the current plan item, work item, attempt, and stable
operation. Human-readable `output` is kept on the execution record; structured
`values` must exactly match `outputs` declared by the handler and become
available to later workflow actions as `{{greeting}}`. Invalid return types,
undeclared values, and missing declared values fail the handler consistently.

A handler that declares `provide` may also declare `validate`, a callable that
receives the handler's own declared values as a mapping and returns an error
message to refuse them or `None` to accept. ww calls it when the agent
supplies the values, before the completion is saved, so a refused value comes
back to the agent as a failed `complete` with that message instead of a
handler failure the operator has to resolve. It runs with no store, no
workspace, and no effects, and it does not replace the check the handler makes
when it runs: `ww/git` declares one for `commit_message` and still checks the
same rule in `git-commit`.

```python
def _subject_error(values):
    if "\n" in values.get("commit_message", ""):
        return "commit_message must be a single line"
    return None

ExtensionHandler(
    "git-commit", _commit, provide=(ProvidedVariable("commit_message"),),
    validate=_subject_error,
)
```

The compiled plan also records the extension API/version, provider source,
source fingerprint, and resolved settings. A resumed run uses those saved
settings and refuses to dispatch if the extension's version, API version, or
provider source has changed, preventing an upgrade from silently changing an
in-flight run. Fixing an extension in place under the same version is allowed,
as it is for ww itself; the fingerprint is recorded for audit only.

An extension may declare `ExtensionVariable` entries to override variables
owned by ww core. It cannot introduce arbitrary global variables or replace
values declared by workflow steps. Only extensions already referenced by the
saved plan participate, preserving lazy discovery. Conflicting overrides are
configuration errors. The Git extension uses this contract to resolve
`__task_workspace_dir` from the primary checkout or its recorded worktree.

`ww/git` is bundled with the installed `ww-agentic-workflows` package, so it is
available in every project, including a `pipx --editable` installation whose
source checkout lives elsewhere. A third-party extension remains project-local.
A project cannot provide a second `ww/git`; duplicate extension IDs are an
error. A handler is one unit of work: a retry re-runs the whole thing, so write
handlers that tolerate that.

## Execute a workflow

```console
ww-agentic-workflows start TASK-123 --workflow task --agent codex --runtime single \
  --init-artifact="Implement the requested change." \
  --model gpt-5 --reasoning high --role manager
ww-agentic-workflows next TASK-123 --model gpt-5 --reasoning high --role manager
# perform the displayed prompt, skill, or slash command
ww-agentic-workflows complete TASK-123 --role worker \
  --artifact "<whole result in Markdown>" \
  --summary-for-next-step "<one or two sentences for the next step>"
ww-agentic-workflows instruction TASK-123 --role worker
ww-agentic-workflows metadata TASK-123
ww-agentic-workflows metadata --project
```

A task has at most one open run. Starting a workflow on a task whose last
run is unfinished, failed included, is refused until that run finishes or
the task is reset, unless the workflow is declared `restartable`: then the
new start abandons the unfinished run of the same workflow and opens a new
one, and the abandoned run stays readable in the task's history with its
items, artifacts, and interactions. This is the natural setting for a
workflow that is run round after round, such as manual testing over shared
items. An unfinished run of a different workflow is never abandoned this
way.

In the `single` runtime one session does every step, so a completion opens
the next agent step itself and prints that step's page: the `next` it would
have run is run for it, preparation hooks included. `next` on a step that is
already open then simply shows it again. When opening the next step needs
something only the agent can give, such as the outcome of an assessment,
the completion prints the pending page that says so. The `auto` runtime
keeps the manager's `next`, because that is where a worker is chosen.

When an outside-of-ww issue has been resolved by an operator, a failed item
can be skipped with `next --force --force-reason "<reason>"`. This is an exceptional operator command:
ww asks for an interactive confirmation and explains that agents must obtain
permission before using it. Answer `no` (or provide no answer) to leave the
failed item in place; answer `yes` only after the operator has approved the
forceful transition.

```console
ww-agentic-workflows next TASK-123 --force --force-reason "Resolved manually" --role manager
# confirmation: Proceed with force? [y/N]
```

Runtime and execution metadata are retained in task instructions. Under
`auto`, compiled requests and manager-selected agent/model/reasoning are
shown and persisted separately; ww itself does not launch agents. Under `single`,
configured workflow and built-in hints are ignored in favor of the current
session. Completion flags can report a bounded delegate for one item without
changing the rest of the assignment.

Human-facing instructions identify themselves as generated by `ww` and name the
reader's current role. In `auto`, the manager receives only a worker
bootstrap command and passes it to the selected worker without task details or
commentary. The worker runs `ww instruction` with `--role worker` to retrieve its
complete, role-specific assignment, then receives instructions for when and
what to hand back. `single` identifies the session as both manager and worker and
states which role to perform without spawning. A short explanation of how `ww`
manages the saved workflow appears only after `start` and an explicit `instruction`;
normal `next` and `complete` responses stay focused on the immediate action.

Successful commands return exit code `0`. Handled `ww` errors and rendered
failed or interrupted workflow states return `1`. Invalid command-line syntax
is still reported by `argparse` with exit code `2`.

The task ID may be omitted from `start`. `task_format` in
`../ww-agentic-workflows.json` then controls generation with `{timestamp}`,
`{digit}`, and/or `{uuid}`; without it, ww uses `TASK-{timestamp}`. It is a
setting of the checkout and of the tracker a repository uses, not of what a
workflow does, so it lives in the JSON settings, at any of their
[levels](#machine-repo-and-local-configuration), and a configured project may
carry [its own](#a-projects-own-extension-settings). A `task_format` key in
any YAML file is an error that names the file and points here.

```json
{"task_format": "TASK-{digit}"}
```

Prefer an explicit ID whenever the request names an external ticket, so the
task matches the issue it works on; `discover` and the embedded agent
instructions say so. Avoid a generated format that imitates your tracker's keys,
such as `FORMS-{digit}` next to Jira's `FORMS-10859`. To rule generated IDs out,
set `"task_format": "explicit"`: `start` and `add-child` then require an ID, and the
only exception is a workflow that obtains its own ID in its first step.

When a task has more than one run, `instruction TASK-123` shows every run and its
summary. Use
`ww-agentic-workflows instruction TASK-123 --run 02-code-review --role manager` for
the detailed instruction and status of one run.

After a run completes, start another workflow with the same task ID to append a
sequential run. Each run retains its own plan, state, artifacts, execution
settings, and summary.

Every non-terminal Markdown instruction shows one role-specific continuation
command. A completion screen may request values for upcoming automatic CLI
handlers in the same assignment; pass each with `--variable name=value`. Before
anything is saved, ww hands each value to the handler that will consume it:
a handler that declares a validator, such as `ww/git`'s `git-commit` for
`commit_message`, refuses a value it would fail on, and the completion fails
with the handler's own message and records nothing, so the agent corrects the
value and completes again. `ww` then
executes those handlers itself before it activates the next worker item or
returns control to the manager. If an automatic command fails, the worker stops
and reports the failure to the manager. The response reports
`awaiting_operator` with `operator_reason: handler_failed`, so the manager
reports it to the ww operator for manual intervention; its instruction lists
the two operator
options, `next --retry` to run the handler again once the cause is fixed and
`next --force --force-reason` to skip it, so the agent can run the one the
operator chooses without guessing. A retried handler that takes provided
values does not replay the values it failed with: it asks for them again
through the ordinary input request, which shows what it was given last time,
so a wrong value is corrected and a right one repeated. `next --force` checks the task state before
it asks for confirmation, and its prompt states what the force will do; a task
that is neither failed, interrupted, nor stopped at a loop limit is refused
without a prompt.

`status TASK-123` is a quick current-state check: it reports only the task ID,
workflow, current step and step state, runtime, and agent/model/reasoning.
Use `instruction` when an agent needs the detailed role-specific guidance.

Markdown and `--json` output are rendered directly from the normalized
instruction record. Project-local `../.ww/templates` files are not a supported
customization mechanism.

### Catalogs, output, and project selection

Catalog commands expose configured and discovered capabilities as JSON; for a
single agent-facing overview use `discover`:

```console
ww-agentic-workflows workflows
ww-agentic-workflows modes
ww-agentic-workflows runtimes
ww-agentic-workflows agents
ww-agentic-workflows projects
ww-agentic-workflows extensions
ww-agentic-workflows artifacts TASK-123
ww-agentic-workflows interrupted
```

`artifacts` returns JSON references for completed step and hook artifacts. Hook
records include their parent step, hook name, and hook phase. Each entry's
`artifact` (or `command_output`) is the stable project-relative reference, and
`path` is its absolute location, which a worker running in a linked worktree
needs because `.ww` lives under the primary checkout.

Lifecycle commands support `--json` when machine-readable output is needed.
By default, `ww` detects the primary Git checkout so linked worktrees share task
state. Pass global `--root /path/to/project` to select a project explicitly.

### Interrupted automatic handlers

If ww is interrupted after it records an automatic command or extension as
started, the operation is shown as `interrupted` because its external outcome
is unknown. `next` only reports that recovery boundary; it never replays the
operation implicitly. Two cases are settled without asking. A command that
had already exited non-zero when ww died is a known failure, not an unknown
outcome: every finished command is recorded the moment it exits, so `next`
reports it as `failed` with the exit code and what it printed, and the
ordinary `next --retry` applies. A command handler declared
`idempotent: true` is replayed by `next` itself, because its author has said
a second run cannot do damage. For everything else, inspect the current state
with:

```console
ww-agentic-workflows status TASK-123 --role manager
```

After checking the external system, use
`ww-agentic-workflows next TASK-123 --role manager --retry` to replay the
unfinished operation. It requires operator confirmation because the external
effect may have already occurred. To advance without replaying it, use `next
--force` with a specific `--force-reason`; this also requires confirmation and
retains the reason with the item record. Agents must ask an operator before
using either exceptional option. Completed command segments remain recorded and
are not replayed.

## Concurrent ww processes

Several `ww` invocations can run against one project at the same time. Each
mutating command — `start`, `next`, `complete`, `reset` — holds an
exclusive lock on its task for its whole duration, so a second process waits and
then acts on the first one's committed state instead of overwriting it. Waiting
is automatic: the blocked process parks until the holder releases, printing one
notice to stderr if the wait lasts longer than a moment.

Reads are not locked. `instruction` and compact `status` answer immediately even while another process is
mid-command, and writes are atomic, so it never sees a partial file.

The order in which waiting processes are served is **not** guaranteed — the
operating system may grant the lock to any waiter. Waiting is bounded by
`WW_LOCK_TIMEOUT` (seconds, default `30`); set it to `0` to wait indefinitely.
Lock files live in `../.ww/locks` and the kernel releases them when a process
exits, so a killed run never leaves one behind. Locks are advisory between `ww`
processes; editing `.ww/tasks/<id>/state.json` by hand is still unsupported.

For a real side-effect smoke test with placeholder agent output, use:

```console
.venv/bin/python scripts/run_workflow_dummy.py TASK-123 --workflow task
```

The runner executes every ww-owned CLI handler for real, including commits. Run
it on a disposable branch or a test repository.

## Initialize and maintain a project

`init` creates a normalized default configuration and project-local agent
instructions. `cleanup` removes inactive lock sidecar files. `reset` deletes one
task's saved state and artifacts and therefore requires explicit confirmation.

```console
ww-agentic-workflows init
ww-agentic-workflows cleanup
ww-agentic-workflows reset TASK-123 --yes
```

## When an automatic handler fails

ww stops the task and hands the decision to the operator. It never retries on
its own and never works around the failure, because both would hide a real
problem behind a green workflow.

The page the agent receives names the command and shows what it printed:

```markdown
### Error

automatic handler failed (1) running: python -m pytest -q

2 failed, 1 passed
```

Either stream is reported — stderr when it has something, otherwise stdout,
which is where pytest, ruff, and mypy actually write. Output longer than forty
lines is tailed, and the complete text stays available through
`ww artifacts <task-id>`.

Under "Operator recovery" the agent is told to hand over: report what failed
and quote the output, say that completed work is saved and that nothing after
the step has run, ask for a decision without making it, and state what happens
next either way. Then stop and wait.

Two routes lead out, and the agent runs whichever the operator picks:

```console
./ww next <task-id> --retry --role manager
./ww next <task-id> --force --force-reason "<reason>" --role manager
```

`--retry` runs the same handler again, for when the cause has been fixed.
`--force` skips it and records the operator's reason in the task, so a skipped
check is visible afterwards rather than forgotten. A loop that hits its
iteration limit escalates the same way, and the force there leaves the loop.

## Installing the ww skills during init

`init` offers the `ww`, `noww` and `ww-rule` skills to every agent integration it knows
about. In a terminal it can redraw, that is one checklist rather than one
question per agent:

```text
Install the ww, noww and ww-rule skills into which agent directories?
  ↑↓ move · space toggles · a all · enter confirms

 > [ ] .agents
   [ ] .codex
   [x] .claude       already present
   [ ] .gemini
```

Directories that already exist start ticked, because having one is good
evidence you use that agent. Nothing is written until you press enter, and
the answers are remembered in `.ww/init-choices.json`, so a later `init` only
asks about agents you have not decided on.

Where the terminal cannot be driven that way — a pipe, `TERM=dumb`, a captured
stdin in a test — ww falls back to plain questions: one for each directory that
already exists, then a single comma-separated question for the agents without
one. `--skills` and `--no-skills` skip the interaction entirely, and
`--no-input` takes the defaults.

`init-choices.json` also remembers which bundled skills you accepted. When a
later ww version bundles a new one, the next `init` asks about that skill
alone, into the directories you already chose, without the agent questions:

```text
ww now ships the `ww-rule` skill. Install into .claude? [Y/n]:
```

Either answer is remembered, so it is asked once; a skill you declined is not
installed later on its own. A project set up before this record counts a
skill already present in a chosen directory as accepted.

The rest of the summary adapts to repeat runs too. The box asking you to allow
ww in your agent's permissions is shown the first time only, and the next
steps for getting started only while `ww-agentic-workflows.yaml` defines no workflow. The
documentation links are always shown.

## Agent hooks

ww's instructions to agents are static, so they cannot say which task is
half done. Agent hooks carry that state into a session. They are gentle by
design: they add a few lines of context or remind once, and nothing is ever
blocked.

| Hook | When it runs | What it does |
| --- | --- | --- |
| `session-start` | A session starts, resumes, or is compacted | Prints a reminder that ww coordinates work here and up to five unfinished tasks, newest first, with their step, workspace, and resume commands. |
| `stop` | The agent ends its turn | Once per step attempt, asks the agent to record an agent-owned step it left in progress. The next stop is always allowed. |
| `interrupt` | The session ends or is interrupted mid-step | Records the interruption without answering, so the next session is told to check the work. |

What `session-start` prints stays in the agent's context for the whole
session, so it is short:

```text
This project coordinates work through ww: `./ww discover` lists its workflows.
Unfinished ww tasks, newest first:
- TASK-16 (task, claudecode) develop: in progress · in ww-worktrees/TASK-16 · resume: `./ww instruction TASK-16 --role manager` · worker: `./ww instruction TASK-16 --run 01-task --role worker`
```

A task whose state cannot be read never silences the hooks. `session-start`
lists the readable tasks as usual and adds one line naming the others:

```text
ww cannot read the state of TASK-20; other tasks and new work are unaffected. `./ww discover` shows why; ask the operator before touching them.
```

`stop` and `interrupt` consider only the tasks that could be read, and
`interrupted` and `lookup` skip a marked task they cannot read.

After a compaction it starts with "Context was compacted; ww's task state is
authoritative." Every unfinished task is listed, whichever agent started it,
with that agent after the workflow, since other sessions' open work is useful
context. A task waiting for the operator stays listed and says so, for
example `awaiting the operator: the work failed`. Work attached to a step as a
hook is named by itself and its step, for example
`update-documentation (a hook of run-tests)`, never by the step alone. Only the main session gets
this context: no hook is registered for a subagent's start, since a worker
receives its bootstrap command from the manager.

`stop` answers "continue" only while an agent-owned step is dispatched and in
progress, not waiting for input or the operator, and only the first time for
that step attempt. Its message names the step and says to run `complete` when
the work is done or `fail` when it cannot finish, and that a manager waiting on
a worker, or a deliberate pause, may simply stop again. Agents that report their
own loop guard (Claude Code's and Codex's `stop_hook_active`, Cursor's
`loop_count`) are always allowed once they have continued.

Which tasks concern a session:

- A session inside a task's worktree or project directory concerns that task,
  whichever agent started it; the most specific workspace wins.
- The root is not a task's workspace, even for a task without a worktree. A
  session in the root, anywhere else, or that names no directory concerns only
  the tasks its own agent started, so a Claude Code session is never reminded
  about a Codex session's step.
- A manager waiting on a worker is not reminded. On a run started with
  `--runtime auto`, a step that may go to a worker (`role: worker`, the
  default) is delegated: the main session's stop skips it, and the worker's
  own stop (`SubagentStop` in Claude Code and Codex, `subagentStop` in Cursor)
  reminds instead. A step the manager performs itself, a `role: manager` or
  interactive step, is still reminded. Antigravity reports no worker
  stop, so its manager is simply not reminded about delegated steps.

### Agents and their files

| Agent | Project file | Native events | Local file |
| --- | --- | --- | --- |
| Claude Code | `.claude/settings.json` | `SessionStart`, `Stop`, `SubagentStop`, `SessionEnd` | `.claude/settings.local.json` |
| Codex | `.codex/hooks.json` | `SessionStart`, `Stop`, `SubagentStop`, `Interrupt`, `SessionEnd` | none; user file `~/.codex/hooks.json` |
| Cursor | `.cursor/hooks.json` | `sessionStart`, `stop`, `subagentStop`, `sessionEnd` | none; user file `~/.cursor/hooks.json` |
| Antigravity | `.agents/hooks.json`, group `ww-agentic-workflows` | `PreInvocation`, `Stop` | none; user file `~/.gemini/config/hooks.json` |

Claude Code's `SessionStart` has no matcher, so it fires for every source,
compaction included. Pressing Esc fires no hook in Claude Code; the end of the
session does. Cursor reports an aborted turn as a `stop` with status
`aborted`, which ww treats as an interruption rather than a reminder.
Antigravity has no session-start event, so ww answers only its first
pre-invocation of a conversation, as an ephemeral message that never
accumulates; it names an interruption through the Stop hook's
`terminationReason`, which ww reads on a best-effort basis. Codex loads project
hooks only once the project's `.codex/` layer is trusted: review them with
`/hooks` in Codex.

### Install, remove, and show

```console
ww-agentic-workflows hook install --agent claudecode
ww-agentic-workflows hook uninstall --agent codex
ww-agentic-workflows hook show --agent cursor
ww-agentic-workflows hook install --agent claudecode --local
```

`install` merges ww's entries into whatever the file already holds and leaves
every other entry alone. ww recognises its own entries by their command, so a
second `install` changes nothing and `uninstall` removes exactly what ww added.
`show` prints the file and the exact snippet, for a machine or an agent where
`install` cannot run. A file that is not valid JSON, or cannot be written,
makes `install` fail with the path and the full snippet to add by hand.

`--local` writes the agent's project file kept out of version control, so the
hooks stay one person's choice. Only Claude Code has one,
`.claude/settings.local.json`; for the other agents `--local` fails and names
their user-level file, which applies to every project on the machine. Without
`--local` the hooks go into the shared project file, which is usually committed,
so everyone who clones the project gets them. When ww's hooks sit in both of
Claude Code's files every hook would run twice, so `install` and `show` print a
notice with the command that removes one copy.

The registered commands call the project's tracked `./ww` launcher: through
`"$CLAUDE_PROJECT_DIR"` for Claude Code, from the project root for Cursor, and
from the repository's top level (`git rev-parse --show-toplevel`) for Codex and
Antigravity. The launcher resolves the durable root through the shared Git
directory, so a session in a task worktree reaches the same state; the launcher
must therefore be tracked for worktree sessions.

The runtime form, `ww hook session-start|stop|interrupt --agent <agent>`, is
what the agents call; it reads the agent's JSON payload on stdin.

### When a hook fails or runs too often

A hook never breaks the agent. On any error, a bad argument, or outside a ww
root, it exits 0 with no output: some agents read exit code 2 as "continue", and
an unexpected message would enter the agent's context. The worst outcome is a
lost hint. When the command cannot start at all, for example a missing
launcher or an untrusted Codex hook, the agent shows its own non-blocking hook
error. A hook that times out is killed by the agent, which carries on.

Running more often than needed is harmless. The stop reminder is claimed under
a lock of its own, so parallel calls, such as Claude Code running matching
hooks at once, remind exactly once. A repeated interrupt overwrites the marker.
Antigravity calls its pre-invocation hook before every model call; only the
first one returns context.

Every hook call is written to the audit log, `.ww/executions.jsonl`, with the
event, the agent, and ww's decision, so a session can be followed there:

```json
{"command": "hook", "hook_event": "stop", "hook_agent": "claudecode", "hook_decision": "reminded: TASK-16", "outcome": "ok"}
```

Decisions read like `context with 2 unfinished task(s)`, `allowed`,
`reminded: TASK-16`, or `marked interrupted: TASK-16`; a failure is logged with
`outcome: error` and the exception type. The agent's payload is never logged.

### Interrupted tasks

When a session ends or is interrupted while an agent-owned step is in progress,
ww writes `.ww/tasks/<task-id>/interrupted.json` with the time, run, step,
attempt, agent, and the reason the agent gave. The next interruption
overwrites it. ww marks the tasks that concern the session, chosen as for the
stop reminder: the task whose worktree or project directory the session worked
in, otherwise the tasks with a step in progress that the same agent started. ww
never commits on an interruption: the tree is often half applied at that
moment, and commits belong to the workflow.

While the marker applies, `status`, `instruction`, and `next` lead with a
notice, and `session-start` adds it under the task's line:

```markdown
> Interrupted: the previous session (claudecode, prompt_input_exit) stopped at 2026-09-28T18:40:00Z during `develop` (attempt 1). Before continuing, check `git status` in the task workspace, review the diff and ww's recorded commits (`./ww extension ww/git commits TASK-16`), and compare them with the step's requirements; then complete, continue, or fail the step.
```

The marker clears once that step attempt completes or fails, or is replaced by
a later attempt, and when the task is reset. It is never cleared merely by being
shown, so a compaction or a new session between reading it and acting keeps
it. A run that is abandoned keeps its marker.

`ww interrupted` lists the marked tasks, newest first, from the markers
themselves; there is no second index to drift:

```console
ww-agentic-workflows interrupted
ww-agentic-workflows interrupted --since 7
ww-agentic-workflows interrupted --all --json
```

`--since` defaults to three days. `discover` and `lookup`, the commands an agent
without hooks runs first, add one line only when the last three days hold an
interruption, and `interrupted_recently` in their JSON:

```text
1 task was interrupted in the last 3 days; run `./ww interrupted` before starting new work.
```

## Choosing a runtime

`single` is the default, and an agent reading `discover` used to be told
little more than that — so it tended to omit `--runtime` and get `single`
every time, including for workflows written to delegate.

`discover` now makes the choice explicit. Any workflow declaring an `agent`,
`model`, `reasoning`, or `profile` is listed with the steps that declare one
and a note to start it under `auto`:

```markdown
- `reviewed` — Cheap triage, strong review. Requests a specific worker on:
  `triage`, `review` — start it with `--runtime auto` so those requests apply.
```

The search covers the whole workflow, not just its top level: nested steps,
loop bodies, per-item stages, and assessment branches all count, as does a
setting on the workflow itself. A workflow that requests nothing is listed
without a note, so the marker means something.

`--json` reports the same thing as `delegation_requests`, the list of step
names carrying a request, empty when there are none.

This is advice, not enforcement. `single` remains right when nothing is
requested, when delegation is unavailable or not permitted, or when the
operator asked the session to do the work itself — and a workflow that always
wants delegation should declare `runtime: auto` rather than rely on the reader.

## Choosing the ww binary

A project names the ww it runs in `../ww-agentic-workflows.json`:

```json
{"executable": "ww-agentic-workflows-dev"}
```

The value is a command on `PATH` or a path. Every command ww prints for that
project starts with it — `ww-agentic-workflows-dev next TASK-1 --role
manager` — and the `./ww` launcher runs it, reading the key each time, so a
project switches installs by editing that one line. Like every setting, the key
may also come from the machine or local settings file, the local one winning,
so one checkout can use a development install without changing the shared
file. Without the key, printed
commands use `./ww` and the launcher runs `ww-agentic-workflows`. `init` writes
`"executable": "ww-agentic-workflows"` when the key is missing, and brings a
launcher an earlier ww wrote up to date; a launcher you edited is left alone.

This is what lets two installs live side by side, for example one checkout for
developing ww itself, switched between branches often, and another kept on
`dev` for use in other projects. pipx gives the second a different global name:

```console
pipx install --editable ~/tools/agentic-workflows
pipx install --editable --suffix=-dev ~/tools/agentic-workflows-dev
```

A project that should use the second then sets
`"executable": "ww-agentic-workflows-dev"`. The update notice, `--version`,
and the audit log keep naming the package, `ww-agentic-workflows`.

Either install also runs inside the other checkout, for example the
development install used for all work in the `dev` checkout. ww recognises a
checkout of its own source by `src/ww/extensions/registry.py` and then uses the
running install's bundled `ww/git`, ignoring the checkout's own `ext/ww/*`
copy. In any other project, an `ext/ww/<name>` of its own is still refused as
a duplicate of the bundled extension.

## Staying current with the ww checkout

ww is installed from a Git clone in editable mode, so whether a newer ww
exists is a local question. Before running the command it was asked for, ww
compares the checkout it runs from against the branch that checkout tracks,
and prints a short notice when it is behind:

```markdown
## A newer ww is available

This checkout is 23 commits behind `origin/main`.

What changed:

- `interact --pause` records that the operator is done for now.
- Items carry custom string fields, set with `--field NAME=VALUE`.

**Tell the person you are working for about this before you continue**, and
let them decide whether to update. To update:

    git -C /Users/you/tools/agentic-workflows pull
```

The notice is announced, never enforced: it is written above the command's
own output, which then runs exactly as it would have. An agent relaying that
output shows the update to the operator first, and the decision to pull is
theirs. Nothing is reported anywhere — the only network call is a `git fetch`
against the remote the user cloned from, made at most once a day, and every
failure in it, including no network at all, leaves the command untouched.

What the notice lists comes from the bullets added to `CHANGELOG.md` between
the two commits, falling back to commit subjects when the changelog did not
change. The branch compared against is whatever the checkout tracks, so
someone following `dev` is told about `dev`.

Each notice is shown once. The record of what was already announced is per
user, in `$XDG_CONFIG_HOME/ww-agentic-workflows/updates.json`, because the
installation is shared by every project on the machine — acknowledging an
update in one project does not raise it again in the next.

```console
ww-agentic-workflows updates            # print the last notice again
ww-agentic-workflows updates --check    # look now, before the next check is due
```

For a command whose output is consumed by a program — the JSON catalogs,
`artifacts`, `items`, anything with `--json` — the notice goes to standard
error instead, so standard output stays parseable.

To switch the check off for a project, set `"update_check": false` in
`ww-agentic-workflows.json`. `WW_UPDATE_CHECK=0` switches it off everywhere,
and `WW_UPDATE_CHECK_INTERVAL` sets the seconds between checks.
