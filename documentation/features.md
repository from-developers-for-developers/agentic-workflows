# ww feature reference

This document is the design guide and feature reference for
`ww-agentic-workflows`. It is one of three authorities for designing workflows:
the [specification](specification.md) says exactly what the syntax, defaults and
failure semantics are, this guide says when to use what and why, and the
[examples](examples.md) are runnable. Start with [Designing a
workflow](#designing-a-workflow). For a short introduction, see
[README.md](../README.md); for internal design, component boundaries, and
persistence invariants, see [architecture.md](architecture.md).

An installation carries the same-version copies of all three: read them with
`ww docs specification`, `ww docs features` and `ww docs examples`.

## Feature overview

- Read-only validation of `ww.yaml`, plus agent-specific workflow
  planning in Markdown or JSON.
- A `ww.yaml` split across imported files, composed in memory.
- User, repo, and local configuration levels, resolved automatically, above
  the workflows ww ships itself.
- A read-only profile of the checkout (`inspect`): branches, activity, fix
  signals, hot paths, manifests and verify commands, and conventions.
- Onboarding state, and setup fragments that ww validates and places in the
  shared or local configuration after asking.
- Built-in learning and setup workflows, started by the `ww-setup` skills:
  ww learns about the operator, team, company and project, designs a setup
  with the operator, and proposes it in full.
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

## Designing a workflow

Start with the smallest workflow that does the job and add structure only for a
concrete reason. This section is the guidance; the
[specification](specification.md) has the exact syntax and the
[examples](examples.md) have runnable versions of everything named here.

### Start linear

A workflow is a list of steps in order. Write the work as steps, give them the
defaults, and stop there until something real asks for more. Items, several
item passes, loops, assessments, persistence, modes and reusable groups each
cost the reader something, so each needs a concrete reason: the work really
splits into independent pieces, a judgment really routes what follows, the
same list really returns every round, a preference really varies per task.
When `items: ~` is enough, do not write item stages; when one pass is enough,
do not add a second. [Example 1](examples.md#1-a-linear-workflow-with-an-automatic-check)
is a complete workflow.

### Steps, handlers and hooks

- An **ordinary step** is the place for anything the workflow visibly does,
  including a command ww runs itself (`argv` or `shell`). It appears in the
  plan, in `status` and in the artifacts, it can fail and be repaired
  (`on_failure: fix`), and readers find it where the work happens. Verifying a
  change is such an operation: write it as a step, after the step that
  changes the code.
- A **handler** is a named definition worth reusing: the same command in
  several workflows, a reusable group of automatic commands, a loop used
  by several workflows. Define one when the second use appears, not before. A step uses it
  with `handler: <name>`.
- A **hook** attaches work to a lifecycle point of steps or workflows
  without appearing as a step: it exists for invariants, things that must
  hold around every matching step or workflow whatever the steps say, such as a
  clean tree before any task starts or a commit when a workflow completes, or
  a check that must pass whenever a code-changing step completes
  (`before_complete` with `on_failure: fix`). A command being automatic does
  not make it a hook, and putting visible operations in hooks hides the
  workflow from the people who read it.

### Conversation or assessment

Both involve a judgment but they belong to different people. An
**interactive step** is a conversation with the operator: discussing a design,
reviewing a change, performing a manual test. The operator talks and decides;
the step ends when their intent is clear, and `choices` only lists the answers
worth offering (`{{ww.choices}}` shows their labels in the instruction; it
guides, it never validates). An **assessment** (`assess`) is the agent's own
evaluation: it looks at the evidence, picks `positive`, `negative` or `mixed`
(or a label you declare) and the workflow routes on that. Use an assessment
to gate or branch on something the agent can decide; use an interactive step
where the operator's say matters. Prefer the compact and standard-branch
forms, and custom labels in `outcomes` only when positive and negative do not
say it.

### Items: independent pieces of work

Use `items` when the work splits into pieces that are independently
completed, checked or reported: review comments, test cases, files to migrate.
`items: ~` gives every item one stage that analyzes, resolves and reports it.
Choose per-item stages (`items.steps`) only when the stages differ. When
several pieces are cheaper analyzed or fixed together but each still has to be
reported on its own, use a shared analysis or fix with one item per
independently reportable source: the workflow has one collection and several
passes over it, and ordinary steps between the passes run once for everyone
([Several passes over the same items](#several-passes-over-the-same-items),
[example 4](examples.md#4-one-analysis-one-fix-one-report-per-comment)).
Make the item the unit that is reported: one item per source comment, even
when a hundred comments get one fix.

### External items: IDs, reconciliation and restarts

When items mirror something outside ww, such as review comments on a pull
request, design for being run again:

- Give each item the source's own stable ID, as the item ID or in a field
  that `identity` requires and `unique` keeps single. A rerun then recognizes
  its comments instead of creating copies.
- Declare `persistent: true` when the same list returns every round. The
  collection step then reconciles the stored items against the source
  (`add-item`, `update-item --text`, `remove-item`) instead of splitting
  again. Identity, text, references and custom fields carry over to the next
  run; analysis, solution, `resolved` and `reported` start clear.
- Keep remote results in item fields, such as the reply ID. Pass the saved
  value to the command that posts, `"{{ww.item.field.reply_id}}"`, so a
  project-owned script can update the reply it already made instead of
  creating another. A per-item command stage that declares
  `saves: item.field.reply_id` stores the command's whole trimmed output
  there, and the last report-stage item is marked `reported` in the same
  commit, only after a zero exit.
- ww makes no exactly-once promise about remote effects. If the process dies
  after a remote reply but before the result is saved, the next attempt runs
  the command again. The handler or script owns idempotency, which is why it
  takes the saved ID.
- `next --retry` after a failed stage, or `start --fresh-items` to forget the
  stored list, are the restart tools; see
  [Items that persist across runs](#items-that-persist-across-runs).

### What needs the agent and what ww does itself

A shell or argv step runs without agent work. Values ww owns, such as
`{{ww.task.id}}`, `{{ww.git.branch}}` and `{{ww.item.field.<name>}}`, are
read by ww when the command runs, so using them never asks the agent for
anything. Only a required agent variable, a `variables` entry of the
`name: description` form, means input is needed: the agent supplies it with
`complete --variable`, and then ww runs the command. Dynamic values go to
commands as `args`, `env` or `argv` entries, never into shell source.

### Modes and rules

Add a mode only for a preference that changes how an agent works, such as
brief updates or asking first, and a rule only for a convention no command
checks, on the steps it governs, with a one-line check where one exists. A
rule never restates its step, a preference or a command, and a step that
needs none gets none. Never redefine a workflow ww ships (`catchall`, `ww-*`).

### Where to put workflows

Personal defaults belong in your global file, the team's in the project's
`ww.yaml`, and your own variation of a project workflow in `ww.local.yaml`; a
lower level replaces a same-named definition. `discover` names each
workflow's source, and where several fit, prefer local over project over
global unless the operator named one
([example 6](examples.md#6-global-project-and-local-variants)).

## Initialize a project

`ww-agentic-workflows init` is additive and safe to run after a partial checkout
or branch switch. It creates only missing files and root configuration keys,
preserves existing content, restores `.ww/tasks`, and reports created and
preserved parts. `--json` and `--no-input` use deterministic defaults for
automation.

The interactive wizard chooses UUID, numeric, or timestamp task IDs, written as
`task_format: "TASK-{{uuid}}"`, `"TASK-{{digit}}"`, or `"TASK-{{timestamp}}"`. When the
project contains `.git/`, it enables `ww/git`, prefers an existing `master`
branch and then `main`, enables separate task branches, and starts with
`feature/{{ww.task.id}}`. It asks for optional workflow-specific branch formats and
whether worktrees should be used. Enabled worktrees default to
`./git-worktrees/{{ww.task.id}}`, and the directory is created immediately.

The settings file init writes holds every root-level setting with its value,
so each option can be found and changed in place; the answers replace the
defaults. Without Git, and with the uuid format:

```json
{
  "enabled": true,
  "runtime": "single",
  "update_check": true,
  "executable": "ww-agentic-workflows",
  "task_format": "TASK-{{uuid}}",
  "limits": {
    "rounds": 3,
    "fixes": 3
  },
  "agent_hooks": {
    "check_unfinished": true,
    "recent_days": 3
  },
  "rules": {},
  "builtins": {
    "init": {
      "model": "cheapest",
      "reasoning": "low"
    },
    "workflow_summary": {
      "model": "auto",
      "reasoning": "auto"
    }
  },
  "workflows": {},
  "projects": [],
  "extensions": {}
}
```

In an existing file init adds the keys that are missing, nested ones
included, with these defaults and keeps every value already there. A key
init does not ask about (`runtime`, `update_check`, `limits`, `agent_hooks`,
`rules`, `builtins`, `workflows`, `projects`) that the user or local settings file
already sets is not written, so a default in the repo file never hides it.

The wizard offers to keep `.ww` out of Git; without consent it only reports
that action. With consent it appends these lines to `.gitignore`:

```gitignore
.ww/*
!.ww/project.md
```

Everything under `.ww` is one checkout's state, except `project.md`, in which
ww records what it learned about the project: it is meant to be committed and
shared. Git cannot re-include a file inside
an ignored directory, which is why the directory's contents are ignored rather
than the directory. A line that ignores the directory whole, as `.ww/`, `.ww`,
`/.ww` or `/.ww/`, would keep the
shared files out too, so every such line is replaced by these lines, written
once where the first one stood. A `.ww/*` line gains the re-inclusions it
lacks, right after it; a re-inclusion only counts after the last `.ww/*` line,
since a later one ignores the file again. Every other line is left alone, and
the file keeps its line endings (CRLF stays CRLF).
The patterns `*ww.local.yaml`,
`*ww.local.json` and `ww-setup.local.yaml` are added without
asking, since [local configuration](#user-repo-and-local-configuration) belongs
to one checkout:
they are appended to an existing `.gitignore` once, never duplicated, and a
missing `.gitignore` is created for them only inside a Git repository. It also reports missing `@WW_AGENT_INSTRUCTIONS.md`
references in `AGENTS.md` and an existing `CLAUDE.md`, and reminds the user to
define workflows when the initialized `ww.yaml` is empty. Equivalent
non-interactive choices are available through `--task-format`, `--worktrees`,
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

`init` also creates the [user configuration
directory](#user-repo-and-local-configuration) when it is missing, and lists it
under "Created or restored".

`init --force` runs every question again, ignoring the answers remembered in
`.ww/init-choices.json`, so agents, skills and hooks can be chosen anew and
more added. It only adds: skills, instruction references, hooks, `.gitignore`
lines and settings keys already in place are kept, never removed and never
written twice, and the new answers replace the remembered ones. Values the
settings files already hold, such as `enabled` or `task_format`, are
configuration rather than remembered answers, so they are not asked again.
`--force` reopens a question only when it can ask it: with `--no-input`, or
without a terminal, every remembered answer stands, a remembered "no"
included, and init only adds what those answers leave missing. A flag such as
`--update-gitignore` still decides without asking.

`enabled` is written to `ww.json` only from the repo level's
own answer: when your user or local settings file already sets it, init
neither asks nor commits that choice for the team.

Unless it was shown before, the summary ends with what to allow so your agents
run ww without asking for confirmation. For each agent set up in the project
whose permission format ww knows, it names the file and the exact entries,
covering the configured `executable`, `./ww` and the `ww` shortcut. For Claude
Code:

```text
  claudecode: merge these entries into .claude/settings.json

     {
       "permissions": {
         "allow": [
           "Bash(ww-agentic-workflows *)",
           "Bash(./ww *)",
           "Bash(ww *)"
         ]
       }
     }
```

Other agents get the command prefixes to allow in their own permission
settings. The notice also says what you trust by doing so: your
`ww.yaml` with its user and local levels. `--force` shows it
again.

Every `init` ends with the next step: run the `ww-setup` skill, which sets ww
up for you, your team and this project (in Claude Code, `/ww-setup`).
`--json` output lists it under `next_steps`.

### Configuration file names

ww reads its configuration from two files at the project root, both named
after the tool: `ww.yaml` for the workflows and
`ww.json` for the project settings.

## Discover how to start a task

`discover` is the entry point for agents. The embeddable agent instructions stay
short and send agents here, so every choice comes from the project's current
configuration:

```console
./ww discover
./ww discover --json
```

The Markdown is deliberately short. It lists the project's workflows with
their descriptions and default modes, each labeled with the configuration
level and source it came from and sorted local, then project, then global
(stable within a level), under this instruction: choose a workflow matching
the request; when several fit, prefer local over project over global; honor an
explicitly requested workflow; if the choice is still unclear, ask the
operator. The preference is guidance, not an automatic selector:

```markdown
- review — [local: ww.local.yaml] Review incoming PR feedback.
- develop — [project: ww.yaml] Implement and verify a change.
- generic-fix — [global: ~/.config/ww/ww.yaml] General bug-fix workflow.
```

After the workflows come the changes no workflow covers (the `catchall`,
started through `lookup`), the configured projects when there are any, the
modes (an automatic mode with where it is always on), and a multiline start
synopsis in which square brackets denote optional arguments. The task ID is
`<task-id>` where the project requires one and `[<task-id>]` otherwise, with
the external-ticket guidance; `--project` and `--branch-strategy` appear only
when projects or branch strategies are configured, with their values. A few
lines explain what is not obvious: explicit modes replace the defaults,
automatic modes apply themselves, `auto` honours per-step worker requests while
`single` records them, and `--model` and `--reasoning` describe your own
session. It ends with the resume and status commands and one optional plan
preview. ww's own workflows are not listed there: it points at `ww workflows`,
the catalog of every workflow. Every unfinished task is listed under
"Unfinished tasks", newest first, in the `session-start` hook's format with any
interruption notice, and `unfinished_tasks` in the JSON carries each one's
`task_id`, `workflow`, `agent`, `step`, `item_status`, `workspace`,
`updated_at`, `resume` command and whether it is `interrupted`. The Rules
suggestion is not part of the Markdown; `rules_notice` in the JSON and the
first page of `start` carry it, see
[How a rule becomes a check](#how-a-rule-becomes-a-check). The JSON keeps every
field, including the built-in catalogs, the roles and the guidance texts, which
are the same concise texts the Markdown shows; `explicit_task_id` states
whether the project requires a task ID. `discover` is read-only and leaves no audit record.

In JSON, each workflow entry also carries `source` and `source_level`, naming
the winning YAML definition and whether it came from the global user config,
project config, or local config. Imported files keep the level of the config
that imported them. `null` for both fields means a contribution without a
configured definition, such as a built-in or the catch-all that no
configuration file defines; one that a file does define reports that file.

A task whose state ww cannot read, such as one written by a build with another
state schema, does not break `discover`. It is listed
under "Unreadable tasks" with the error, and `unreadable_tasks` in the JSON
carries each `task_id` and `reason`. Other tasks and new work are unaffected;
commands addressing that task keep failing with the same error, and whether to
repair, reset, or delete its directory is the operator's decision.

```markdown
## Unreadable tasks

- `TASK-20` — invalid task state .ww/tasks/TASK-20/state.json: unsupported plan snapshot schema: 3

Other tasks and new work are unaffected. Commands addressing these tasks fail with the error shown; ask the operator, whose choice it is to repair, reset, or delete each task directory.
```

Set `"runtime": "auto"` in `ww.json` to make `auto` the runtime
`start` uses when `--runtime` is omitted; `discover` then marks it as the
default. The setting lives in the JSON file because whether delegation is
available depends on the environment ww runs in, not on the workflows. A
workflow that only makes sense one way, such as a manual-testing workflow
whose every step is a conversation with the operator, may declare
`runtime: single` itself; that outranks the project default, and the flag on
the command line still wins over both.

Set `"enabled": false` in `ww.json` to switch ww off for a
project. `discover` then says only that ww is disabled and that the agent must
not use it, and `start` refuses to create a task.

Set `"enabled": "on_request"` to keep ww available but out of the way: an
agent uses it only when the user explicitly asks for it (says to use ww, names
a ww task, or invokes the `ww` skill), and otherwise works without ww and
without asking. `discover` opens with that rule, then lists the workflows so
an explicit request can proceed, and its JSON carries `"enabled":
"on_request"`. `start` and `lookup` work as usual, `lookup` reminding the agent
to go on only for an explicit request, and the `session-start` hook says that
ww is used here on request only. The static agent instructions and the `ww`
skill defer to `discover` for this choice. `init` asks which of the three
values to write, explaining each; without a terminal it writes `true`.

```json
{"enabled": false, "extensions": {}}
```

## Onboarding state

ww keeps a little state about how far it is set up, each part where it
belongs:

| Key | Where | Means |
| --- | --- | --- |
| `explain` | `state.json` in the user configuration directory | whether the operator wants the agent to narrate what ww does while it learns; optional, absent until they say so |
| `setup.done` | `.ww/metadata.json`, as `ww.setup.done` | whether ww was set up in this project |
| `learned.project` | `.ww/metadata.json`, under `ww.learned` | when ww last learned about the project |

The project keys live in ww's own `ww.` namespace of project metadata, which
no workflow can save into, so they never collide with a workflow's values.

```console
./ww onboarding
./ww onboarding --json
./ww onboarding --set explain=true --set learned.project=now
```

`--set` is repeatable and takes a known key: `explain=true|false`,
`setup.done=true|false`, `learned.project=now` or an ISO
timestamp. An unknown key or a malformed value is an error, and nothing is
written unless every assignment is valid. Setting records the operator's stated
preference, so it asks for no confirmation; it appears in the audit log, while
showing does not.

While `setup.done` is not recorded, `discover` adds a short "Onboarding"
notice: ww has not been set up in this project, setup is optional and never
blocks ordinary work, and the agent mentions the `ww-setup` skill only when the
operator asks to set ww up or what ww can do. It asks no question and starts
nothing. Its JSON carries `onboarding` with `setup_done`, `explain` and the
`guidance` lines. Under `"enabled": "on_request"` the notice only informs.

## Apply a proposed setup

The setup skills propose configuration from what ww learned: workflows, modes,
handlers, hooks, rules, and settings. They never edit ww's configuration files
themselves; they write the proposal to a scratch file and hand it to ww:

```console
./ww setup apply proposal.yaml --for me --dry-run
./ww setup apply proposal.yaml --for me --yes
```

The file is a [setup fragment](specification.md#setup-fragments). `--for me`
places it in your local files, kept out of version control
(`ww-setup.local.yaml`, imported by `ww.local.yaml`, and
`ww.local.json`), so you can try it first; `--for team`
places it in the shared files (`ww-setup.yaml`, imported by
`ww.yaml`, and `ww.json`), committed with
the repository. Running it again refines the same setup file: definitions of
the same name are replaced, the rest kept, and a hook identical to one already
in its phase is skipped (the summary says so), so applying a fragment twice
adds nothing. The file is written as readable YAML, as a person would write
it: block style, a bare `- run-tests:` for a handler shorthand, long text
folded with `>-`, short lists such as `argv` inline, and a blank line between
sections and between workflows, so it can be reviewed and edited by hand.

ww first validates the fragment in memory: it loads the configuration as any
command would, reading the planned contents in place of the files they
change, so validating never touches the project and it only ever shows a
change that works. It then prints what it will write, file by file:

```text
`ww setup apply --for team` writes for the team: files shared through the repository:
- ww-setup.yaml (new): adds workflow `review`, mode `gently`
- ww.yaml: adds ww-setup.yaml to imports
- ww.json: sets runtime
```

and asks `Apply it? [y/N]` at a terminal. Without one, as in an agent's shell,
it needs `--yes`, given once the operator agreed; `--dry-run` prints the same
and writes nothing, and `--json` reports the files and changes for a program.
A setting that already holds a different value refuses the whole apply,
listing each conflict: ww never overwrites one. The files are written once,
after confirmation; a file that is a symbolic link is written through to its
target and keeps its permissions. When a write fails, ww puts back every file
it already wrote, removes its temporary file, and reports the error. Nothing
is committed.

`--dry-run --inspect <workflow> --agent <agent>` also compiles that workflow as
the change would leave it and prints its execution plan, so a draft can be
checked, in memory, before anything is asked or written: that automation is
ww-owned, that item scopes are valid, and that the operator meets only the
conversations intended.

### Change a workflow that is already defined

`setup apply` adds definitions to ww's own setup files. To change a workflow
the configuration already defines, in its root `ww.yaml`, in a file it
imports, or at another level, write the complete changed workflow alone in a
fragment (`workflows` holding that one entry, named like the workflow) and
update it where it is written:

```console
./ww setup update review proposal.yaml --dry-run --inspect review --agent codex
./ww setup update review proposal.yaml --level project --yes
```

The definition that wins in the composed configuration is the one edited, and
the preview names its file and level. `--level local|project|global` selects
that level's definition instead, and ww refuses when a higher-precedence
definition hides it, because an edit there would report success and change
nothing; it tells which definition is in force. Only that list item's lines
are replaced, in ww's YAML style: the rest of the file, other definitions and
comments included, stays byte for byte. Comments inside the replaced entry
are lost, and the preview says so. ww checks that the file reloads as the old
data with exactly that entry swapped, validates the whole configuration in
memory, and checks that the workflow now comes from the file it wrote. The
preview is a diff; `--yes`, `--dry-run` and `--json` work as for `apply`. A
workflow only ww ships, or one defined nowhere, is refused: define or
override it with `setup apply`. Nothing is committed.

## Inspect the project

`ww inspect` prints a read-only profile of the checkout: facts about how the
work is organised, each with the command or file it came from, or "not
found". It reads the working tree and the local Git history and nothing
else: no fetch, no network, nothing outside the checkout, and it writes no
file. It needs no `init`, so the setup workflows can run it on a fresh
project. `--json` prints the same profile as data, and `--commits N` reads
the last `N` commits instead of 300.

```console
$ ./ww inspect
# Profile of shop

Read-only facts about this checkout; each names where it came from.

## Repository

- Default branch: main (git symbolic-ref origin/HEAD)
- Integration branch: dev (merges on refs/remotes/origin/dev, git log --first-parent --merges)
- Branch patterns: feature/ 41, hotfix/ 6, release/ 3 (git branch -r, merge subjects)
- Merge commits: 22% (66 of 300 commits, git log -n 300)
...

## Fixes

- Fix share: 18% (42 of 234 non-merge commits, subject starts with fix, fixes, fixed, hotfix or revert, or names a regression, git log -n 300)
- Paths fixes touch: src/cart/totals.ts 9, src/cart/tax.ts 5, ... (paths the fix commits touch, git log -n 300 --numstat)
...

## Layout

- Manifests: package.json [12 scripts], apps/web/package.json [6 scripts] (root and two levels down, git ls-files)
- Verify commands: test: `pnpm run test` from package.json; lint: `pnpm run lint` from package.json (scripts and targets named test/lint/typecheck/check/format/build in the manifests)
...

## Conventions

- Ticket prefixes: SHOP 188, OPS 12 (git log -n 300; git branch -r, merge subjects)
- task_format candidate: `SHOP-{{digit}}` (the most frequent tracker key prefix)
- commit_format candidate: `{{ww.task.id}}: {{commit_message}}` (task ID first when half the subjects start with a key)
...
```

| Section | Facts |
| --- | --- |
| Repository | Default and integration branch, branch lanes (`feature/`, `hotfix/`, `release/`, …) with counts, remotes, shallow clone, share of merge commits and the merge style, pull request signals (template, `CODEOWNERS`, "Merge pull request" subjects), tags and the median days between the last ten. |
| Activity | Commits read, contributors and those active in 90 days, commits per week over the weeks the history spans (one to twelve, so a young history is not read as sparse), median files and lines per commit, median branch lifetime. Team shape: solo (one active contributor), small (2–5) or team (6+); cadence: daily (a median of five commits a week or more), weekly (one or more) or sparse. |
| Fixes | Share of non-merge commits whose subject starts with fix, fixes, fixed, hotfix or revert (after an optional tracker key, so `fix:` and `fix(scope):` count) or names a regression — "Add the fix loop" is not a fix; the five paths they touch most; the five most recent such subjects. |
| Hot paths | The ten most-changed files. |
| Layout | Manifests at the root and two levels down (`package.json`, `pyproject.toml`, `Makefile`, `composer.json`, `go.mod`, `Cargo.toml`, `Gemfile`, `pom.xml`, `build.gradle`), CI files, the verify commands as exact argv where a manifest names them (`package.json` and `composer.json` scripts, `Makefile` targets, `[tool.pytest]`, `[tool.ruff]` and `[tool.mypy]` in `pyproject.toml`), monorepo signals and the candidate `projects` they name. Sibling repositories a README links to are named, never read. |
| Conventions | Tracker key prefixes in subjects (upper case) and branch names (any case, so `hotfix/task-3` counts as `TASK`), with the `task_format` candidate; the share of subjects led by a key and of conventional-commit subjects, with the `commit_format` candidate; agent instruction files present. |

Outside a Git repository only the layout and the agent instruction files are
reported, under a line saying the Git facts are unavailable. An empty
repository, a shallow clone or one without remotes is reported as it is; a Git
call that fails or takes too long leaves only its own fact not found.

## Setting ww up: learning and suggestions

ww can learn how the project works, and design a setup for the project with the
operator from that; it learns only the project, never who uses it. The
`ww-setup` skill guides the operator through it. `discover` mentions the skill
only when the operator asks to set ww up or what ww can do here, while
`setup.done` is not recorded (see [Onboarding state](#onboarding-state)). The
work itself is done by ww's own [built-in workflows](specification.md#built-in-workflows),
shipped as YAML and started like any workflow; each skill is a thin starter
for one of them.

| Skill | Workflow | Does |
| --- | --- | --- |
| `ww-setup` | — | The guide. Asks once, in its opening message, which path to take: Express (learn-project, then suggest told to derive defaults) or Guided (learn-project, then suggest, which asks a few process questions), and records `setup.done` at the end, also when everything is declined. Narration (`explain`) is never asked; it is recorded only when the operator says they want it. Once set up, it offers the ones below instead. |
| `ww-learn-project` | `ww-learn-project` | Learns the repository: what it is for, and how its work is organised. It reads an existing `project.md` first, so a rerun refreshes it. It starts from [`ww inspect`](#inspect-the-project)'s profile and reads only what the profile cannot see, such as what `AGENTS.md` allows and what the pull request template demands. First the setup facts, each with its evidence or "not found": the default and integration branches and the branch patterns in use, merge or rebase, required pull requests, the test, lint, type check, format and build commands as exact argument lists, how and where those commands run (on the host or through a container exec, virtual environment or task runner, given as an exact argument list, and whether each worktree has its own environment), the tracker's key format as a `task_format` candidate, the commit convention as a `commit_format` candidate, CI gates and releases, the workflows and conventions already in use, and what agents may already do. Then agent tooling, infrastructure and stack, other conventions, and recurring pitfalls, starting from the profile's fix commits and adding review comments where `gh`, `glab` or a tracker is signed in, each as a candidate rule with its evidence and, where a command could verify it, a check. `project.md` keeps the trimmed profile as its "Profile" section, above "Setup facts". Changes no project file but `project.md`. |
| `ww-suggest` | `ww-suggest` | Gathers the setup facts from `project.md`, running `ww inspect` when it has no profile, and reads the [specification](specification.md), this guide and the [examples](examples.md) for what a setup can be made of. In the guided path it asks a few process questions: what is painful, what outcome would help, where the operator wants to be involved and what may run automatically, skipping what the project already answers. Then it designs a minimal setup with the operator in one set of questions, proposes it in full, with the step features each kind of work calls for (an `items` step for cases, interactive steps and the operator page for what the operator performs, a document or item fields for a template such as a test case, a loop for work repeated until a condition holds), shows it with `setup apply --dry-run`'s list of changes, walks through a realistic task, and places it on confirmation; see below. It follows one method per proposed workflow: trigger, result and operator involvement; the smallest structure; concise YAML with a walkthrough and a failure path; validation with the compiled plan inspected; then apply. Commands come from repository evidence, never guessed. |
| `ww-refresh` | `ww-learn-project` | Runs the project learning again; see below. |
| `ww-wizard` | — | Shapes the setup with the operator: asks which of four branches (create a workflow, change an existing workflow, create or improve rules, choose an approach) unless the request says, adapts the depth of its questions, reads the `ww docs` sections, challenges needless complexity with a simpler alternative, drafts, validates with `--dry-run --inspect`, and places the change with `setup apply` or, for an existing workflow, `setup update` at the level `discover` reports. Rule work goes to the rules skills. |
| `ww-solve` | `ww-solve` | Listens to a problem, proposes the smallest change that addresses it, using the step features that fit the kind of work, and applies it for the operator or the team on confirmation. |
| `ww-rules-from-artifacts` | `ww-rules-from-artifacts` | Reads the artifacts of chosen steps across recent tasks and proposes rules from the lessons that recur, added with `rules add` on confirmation. |
| `ww-scriptize` | `ww-scriptize-rules` | Turns every rule with no check yet into checks for the whole project: collects the `unscriptized` rules and groups them into the fewest checks, agrees them with the operator in one conversation, builds and proves them (a deliberate violation, then a sample of real files, with real violations reported and a baseline offered), previews each `rules convert` and `rules decline` with `--dry-run` in a second conversation, and records the approved ones in a step of its own after it. It automatically creates a branch from `extensions.ww/git.base_branches.default`, which must be configured, and follows ww/git worktree settings, so its tool installs and configuration land on a branch of their own. The store it records into, `ww-rule-automation.json`, is in the main checkout: commit it there with, or right after, merging the run's branch; until then `is-git-clean` refuses the next task. |
| `ww-automate` | `ww-automate` | Looks at a step's instruction and past results for mechanical work a script could do, and proposes the script and a hook (or, for a workflow the configuration defines, the workflow with the step turned into a command step, placed with `setup update`); applies on confirmation. |

ww never interviews the operator about who they are, their role, their team
or their company. What a setup needs of the operator is a few questions about
the process, asked in `ww-suggest`: they are interactive steps, which open with
the questions in one numbered message, then converse until the operator's
intent to finish is clear, asking naturally if it is ambiguous. A pick between
a few answers goes through the host's native question tool when available.
The answers are ordinary conversation: they shape the proposal (modes, operator
stops, review, automation) and are not written to a file of their own. Every
question can be skipped, and setup never blocks ordinary work. `project.md` and
the setup are shown before they are written, and the shared file stays
uncommitted until the operator commits it. These workflows declare `runtime:
single` and give every step to the session that talks to the operator (`role:
manager`), so they work in agents without subagents. When `explain` is `true`,
the skills start them with the built-in `ww-narrate` mode, whose steps tell
the operator what each one does and why.

**Express or Guided.** The opening question offers two paths. Guided runs
`ww-learn-project`, then `ww-suggest`, which asks the process questions; it
takes about five replies: the opening question, the project review, the
process answers, the design and "apply". Express runs `ww-learn-project`, then
`ww-suggest` started with the requirement "Express setup": it asks no process
question, states the defaults the project supports, and asks only a
consequential choice the project leaves open. Both keep project learning: an
existing `project.md` is read first and refreshed, never silently replaced by a
generic template.

**What `ww-suggest` proposes.** Its `design` step asks, in one message with
a default for each answer taken from the profile, what the setup turns on,
and states as decided what the profile already answers: the integration
branch and the lanes, from the branch patterns present (no `hotfix` lane
without hotfix-like branches, unless the operator asks), the commands that
verify a change, their order and how they run, whether agents commit (following the
project's convention) and push (never), worktrees (on by default when several
contributors are active and the operator works on parallel tasks), the task ID
format, who reviews (for a solo project an agent self-review step rather than
an interactive review; for a team the operator keeps the review), the step
features a workflow whose work is not a plain code change uses, which of
the process answers become modes or operator stops, `projects` when the
layout found candidates (it asks for the sibling repositories' paths and never
scans them), a rule for a fix-prone path only when the fixes show a repeated
cause, and whether the setup is for the operator alone or the team. The proposal then
follows the project, within what this guide and the [specification](specification.md)
describe: `ww/git` settings for the branching and commit format, and one workflow
per lane, with `inherit` where lanes differ only in their base branch. The
verify commands become visible command steps of the workflow by default, as
[Designing a workflow](#designing-a-workflow) says; a check on every completion
of a step is proposed only where the project treats it as an invariant. Modes
are for preferences, and rules only for conventions no command can check. The
step features a workflow needs follow its kind of work and are added only for a
concrete reason: a manual-testing workflow, for example, collects the test cases
as an `items` step, puts each case before the operator on the operator page
(`interactive: page` with `pass` and `fail` choices), and keeps the test case
template as a document the steps save, with per-case values as item fields.
Every command the proposal carries, in a handler, a step, a hook, a rule's
check or a script, is written for the directory ww runs it from, the task's
worktree when worktrees are on, through the wrapper `project.md` records for the
project's commands, and never names the main checkout. `ww-solve`,
`ww-automate`, `ww-rules-from-artifacts` and the `ww-rule` skill follow the
same convention. A small project gets one lane and a handler or two. [Example
21](examples.md#21-what-ww-suggest-proposes-for-a-node-project-with-devmain-and-a-jira-like-tracker)
shows one realistic shape. It is presented section by section, changed as the
operator asks for up to three rounds, and placed only on "apply".

Every piece of the proposal carries its evidence in one clause, so the
operator can see why it is there: "`hotfix` lane: 14 `hotfix/*` branches
merged this year", "`run-tests` runs `npm test`: `package.json` scripts".
Before asking to apply, `ww-suggest` walks through the main lane in ten lines
or fewer, its steps in order with what an agent does at each; after applying,
it shows the first page of `ww plan --workflow <main lane>` as what an agent
gets on the first task. When
`project.md` is missing, `ww-suggest` says the proposal will be weaker and
offers to learn first, and a later `ww-setup` run recommends learning the
project before anything else.

What ww learns goes into one file it keeps for its own use, `project.md` in
`.ww/` at the project root, written by `ww-learn-project` and shared once
committed. It records evidence and operational facts about the repository;
preferences about workflows belong in the resulting proposal and configuration,
and ww keeps no replacement dossier about the operator. It is the built-in
document `project`, so `{{ww.documents.project}}` names it in any workflow, and
it resolves against the project root even for a task working in a Git
worktree. It starts with this remark, which tells any other agent
to leave it alone:

```markdown
<!-- This file is maintained by ww for ww's own use. Do not use it for anything else. If you are an agent that is not doing ww work, ignore this file. -->
```

`init --update-gitignore` keeps `.ww/` out of Git except `project.md`. ww never
commits it: the last step of `ww-learn-project` names the file it left for the
operator to review and commit, and records when ww learned with `ww onboarding
--set learned.project=now`. `ww-suggest`, `ww-solve`, `ww-rules-from-artifacts`
and `ww-automate` read `project.md` where it exists and keep their proposals
within the project's conventions.

The proposals never touch ww's configuration files through the agent. A
workflow writes its fragment to the task's `setup_proposal` document
(`.ww/tasks/<task-id>/setup-proposal.yaml`) and places it with
[`ww setup apply`](#apply-a-proposed-setup): `--for me` into the local files,
for trying a setup alone, `--for team` into the shared ones. Running
`ww-suggest` again later and choosing to share offers the same setup to the
team. A fragment applied with `setup apply` only adds definitions; to change a
workflow the configuration already defines, wherever it is written, the
workflow proposes the complete changed workflow and places it with
`setup update` (see [Change a workflow that is already
defined](#change-a-workflow-that-is-already-defined)). Rules proposed from past artifacts are written with
`rules add`, like the `ww-rule` skill's.

**Refreshing.** Every learning step reads the existing file first, asks only
what is missing or may have changed, keeps what still holds, updates what
changed, and marks what no longer holds as superseded with the date. So
refreshing is running `ww-learn-project` again, which is what the
`ww-refresh` skill does after showing when ww last learned the project.

**Git hooks.** The learning and setup workflows create no branch or worktree
and commit nothing themselves. `ww-scriptize-rules` has its own Git hooks: it
requires `extensions.ww/git.base_branches.default`, always creates a branch
even when `separate_branch` is false, follows the worktree settings, commits
its changes and returns to the base when worktrees are off.
A project's global hooks filtered with `workflows:` to its
own workflows, as the `ww/git` start hooks usually are, do not reach them; a
global hook without a `workflows` filter does, so list your workflows in it
if it creates branches or worktrees.

**Switching them off.** Each is a built-in workflow, switched off by name in
`ww.json`; the documents and the mode stay while any of them
is enabled, and a recommendation of a switched-off one (`ww-learn-project` recommends
`ww-suggest`) is dropped:

```json
{"workflows": {"ww-solve": {"enabled": false}, "ww-automate": {"enabled": false}}}
```

A workflow of the same name in any `ww.yaml` level replaces the shipped one.
The `ww.json` workflow settings only switch built-ins on or off; scriptizing
needs no lane setting.

## The catch-all workflow

Unless `enabled` is `"on_request"`, every change to files goes through ww,
including the small ones that fit no workflow: renaming a helper, fixing a typo, adjusting a setting. For those, ww
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
`12345` and `foobar-12345` both mean `FOOBAR-12345` under `FOOBAR-{{digit}}`, and,
failing both, the existing tasks whose ID ends in it after a separator, so
with tracker keys `12345` finds `FOOBAR-12345`. Then it answers with one next
step:

| Found | Next step |
|---|---|
| One task, with an unfinished run of another workflow | Continue that run: `./ww instruction FOOBAR-12345 --role manager`. The change belongs to it. |
| One task, otherwise | Start `catchall` on it; the printed `start` command is ready to run. |
| Several tasks | Ask the operator which one, then continue or start on it. |
| No task | Ask the operator to confirm creating the ID the reference names, `FOOBAR-99` for `99`. |
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

The catch-all is one of ww's [built-in workflows](specification.md#built-in-workflows),
shipped as YAML with ww. A project replaces it by defining its own workflow
named `catchall` in `ww.yaml`, or switches it off in
`ww.json`:

```json
{"workflows": {"catchall": {"enabled": false}}}
```

## Validate configuration and plan a workflow

`ww-agentic-workflows` turns `ww.yaml` into an explicit, inspectable
execution plan. Skills and slash commands are discovered only from the project's
shared `.agents/` directory and the selected agent's directory.

```console
ww-agentic-workflows lint
ww-agentic-workflows plan --workflow task --agent codex
ww-agentic-workflows plan --workflow task --agent codex --task-id TASK-123 --json
```

The workflow and agent options have `-w` and `-a` short forms. On `start`,
runtime also accepts `-r`.

`lint` validates the complete `ww.yaml` configuration without needing a
workflow or agent. It and `plan` are read-only: neither creates task state,
artifacts, commands, or execution log records. Markdown is for people; `--json`
returns a stable representation for tools. `--agent` is required because
automatic resolution depends on the agent’s project-local skills and slash
commands. `--task-id` is optional; when
omitted, `{{ww.task.id}}` remains visible as an unresolved plan dependency.

## Split ww.yaml into several files

A large configuration can be split across files. `ww.yaml` stays the
required root file and lists the others under `imports`, its first key (only
`extends` may come before it). Paths are relative to the directory of the file
that lists them; every other relative path in the configuration still resolves
against the project root:

```yaml
# ww.yaml
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

An imported file can define anything `ww.yaml` can, except further
imports, so every file is listed in one place. Files apply in order, the root
file last, and a later definition overrides an earlier one of the same name:
here `ww.yaml`'s `test` handler replaces the shared one, and
`local.yaml`'s `hotfix` workflow replaces `shared.yaml`'s. Named entries of
`workflows`, `modes`, `documents`, `handlers`, and `profiles` are replaced one
by one, other entries from every file are kept, and `hooks` from every file are
combined, later files' entries running after earlier ones.

Overriding is never an error. `lint` reports each override as a notice:

```console
$ ww-agentic-workflows lint
ww.yaml is valid.
Configuration files: workflows/shared.yaml, workflows/local.yaml, ww.yaml
Notice: workflow 'hotfix' from workflows/shared.yaml is overridden by workflows/local.yaml.
Notice: handler 'test' from workflows/shared.yaml is overridden by ww.yaml.
```

ww composes the files in memory on every command into one document and reads
it exactly as a single `ww.yaml`, so every other rule applies unchanged
and there is no cache to refresh. `init` sees keys and workflows defined in
imported files too, and does not add them to `ww.yaml` again. The
[specification](specification.md#imports) has the exact rules.

## User, repo, and local configuration

Both configuration files come in three levels, which ww finds and applies on
every command, top to bottom:

1. user: `ww.yaml` and `ww.json` in
   `~/.config/ww/` (under `$XDG_CONFIG_HOME` when that is
   set), yours alone and shared by every one of your projects;
2. repo: `ww.yaml` and `ww.json` in the
   project root, checked in;
3. local: `ww.local.yaml` and
   `ww.local.json` in the project root, for one checkout and
   kept out of version control.

The repo `ww.yaml` stays required: a user file alone never
makes a directory a ww project. `WW_USER_CONFIG_DIR` names another user
directory; the test suite points it at an empty one so a developer's own
configuration never leaks into tests. `init` creates the user directory when
it is missing and lists it under "Created or restored".

A lower level extends the levels above it with the same rules as
[imports](#split-wwyaml-into-several-files), and wins: named
workflows, modes, documents, handlers, and profiles are replaced one by one,
hooks are added per phase, and other keys take the lower value. Each level can
use `imports` of its own, resolved next to the file that lists them.

```yaml
# ~/.config/ww/ww.yaml
handlers:
  - name: test
    argv: [pytest]
workflows:
  - review: Review a change.
    steps:
      - review: Review it.
```

```json
// ww.local.json
{"task_format": "DEV-{{digit}}"}
```

With the repo file defining its own `test` handler and its
`ww.json` a `task_format`, the project gets the user's
`review` workflow, the repo's `test` handler, and the local task format. `lint`
lists the files it read, user to local, the YAML files first and the JSON
ones after, and names the file behind each YAML override; `plan` ends with the
same list:

```console
$ ww-agentic-workflows lint
ww.yaml is valid.
Configuration files: ~/.config/ww/ww.yaml, ww.yaml, ww.json, ww.local.json
Notice: handler 'test' from ~/.config/ww/ww.yaml is overridden by ww.yaml.
```

A configured project's own `ww.json` and
`ww.local.json` add one more level for tasks working there,
limited to the `extensions` section and `task_format`; see
[A project's own extension settings](#a-projects-own-extension-settings).

A level that should not build on the ones above sets `extends: false` in its
root file or any of its imports; that level then starts afresh, and `lint`
reports each file it leaves out. `extends: true` is allowed and changes
nothing.

```yaml
# ww.local.yaml
extends: false
workflows:
  - task: My own way of working.
    steps:
      - develop: Do it.
```

```console
$ ww-agentic-workflows lint
ww.yaml is valid.
Configuration files: ww.local.yaml, ww.json
Notice: ~/.config/ww/ww.yaml is not applied: a lower level sets extends: false.
Notice: ww.yaml is not applied: a lower level sets extends: false.
```

The JSON settings are always deep-merged and take no `extends` key: nested
objects merge key by key, while strings, numbers, booleans, lists, and `null`
from a lower level replace the value above. A user file can, for example,
set `{"runtime": "auto"}` for every project while one checkout's
`ww.local.json` sets
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
optional workflow hooks.

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
    variables:
      - name: commit_message
        description: A concise commit message.
    argv: [git, commit, -m, "{{ww.task.id}}: {{commit_message}}"]

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
        artifact_from: init
```

Each workflow run gets its own `init`, including handoff targets and child-task
workflows. `ww plan` includes it in the exact execution order. `start` requires
`--requirements` and stores that normalized requirements text immediately in
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

`assert` adds a list of output conditions to the root command action, all of
which must hold: `assert: [empty]`, or `assert: [{equals: clean}]`. A handler
is one action; use a hook's ordered `handlers` list for multiple commands.

`idempotent: true` declares that running the handler again is harmless. It
changes one thing: when ww is interrupted while the handler runs, the next
locked `next` replays the interrupted and unrun commands under the same
operation identity and carries on, instead of stopping at the recovery
boundary for an operator decision. The default is `false`, which keeps the
unknown outcome until `next --retry` or a checker settles it; see [Interrupted automatic handlers](#interrupted-automatic-handlers).
Declare it on test runs, linters, and checks that only read; leave it off
anything that publishes, commits, or sends. `lint` rejects it without `argv`
or `shell`, the saved plan carries it, and `plan` shows it under
**Recovery**:

```yaml
handlers:
  - name: tests
    argv: [python, -m, pytest, -q]
    idempotent: true
```

### Handler types and ownership

Set `kind: skill`, `kind: slash_command`, `mcp: <connection>`, `argv`, `shell`,
or `kind: prompt` to select a handler kind explicitly; the registry form
`action: {type: <action>, ...}` selects any registered action by its
identifier. The handler description is the instruction for prompt and MCP
work. Use an `assess` step when a decision must control workflow routing.
Without an explicit action, resolution is deterministic:

1. A matching project-local skill is an agent skill handler.
2. A matching project-local slash command is an agent slash-command handler.
3. Otherwise the name/description becomes an agent prompt.

### Automated handler groups

A reusable handler can contain `handlers`, an ordered sequence of actions
that ww executes itself. Use `steps` for a sequence that includes agent work
or needs separate step lifecycles.

```yaml
handlers:
  - build: ~
    shell: npm run build
  - verify-build: ~
    on_failure: fix
    on_failure_instruction: Fix the reported build or output errors.
    handlers:
      - build: ~
      - argv: [test, -d, dist]

workflows:
  - name: task
    steps:
      - verify-build: ~
```

Members run in order under the enclosing step or hook's lifecycle. They may
be inline automatic actions, catalog references (including later declarations),
or nested automated groups. Empty groups, reference cycles, agent-owned
actions, and actions requiring agent input are rejected. A group cannot also
declare a direct action or a step container.

Groups supply defaults for the working directory, repair worker guidance,
failure policy, and failure instruction; members can override them. Each
member uses normal ww execution and recovery. If a command with
`on_failure: fix` fails, its agent repairs the cause and ww retries that member,
preserving successful preceding members.

Hooks can reference these groups and keep their existing phase and filters.
Eligible `before_complete` members with `on_failure: fix` become completion
checks. Existing hook `handlers` lists remain compatible, including their
support for agent actions; reusable automated groups require every member to
be fully automatic.

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

The standard branches can also sit directly beside `question`:

```yaml
- assess:
    question: Does recent development warrant refactoring?
    positive:
      handler: refactor-plan
    negative:
      steps:
        - record: No refactoring is needed now.
```

Direct branches and `outcomes` cannot be combined; use `outcomes` for custom
labels.

The agent sees the choice before it answers: the assessment's page lists each
outcome and what it does, for example "`negative` — ends the workflow here".
Once the assessment is complete, the next page asks for the outcome and shows
one `next --outcome <label>` command per outcome, never a plain `next`, which
ww would refuse. An outcome made of an automatic command runs only after its
outcome is chosen; ww pauses at a pending assessment rather than running any
branch. A delegating manager chooses it itself; no worker preview is
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
- A CLI handler with `variables` entries of the `name: description` form is
  still automatic, but first has
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
  --requirements="Implement the requested change." \
  --mode economy --runtime auto --model gpt-5 --reasoning high
```

Each agent step's page lists the modes it works in under **Modes**, with
their descriptions, so the guidance reaches whoever performs the step. The
same pages get modes as get rules: not `init`, hooks, verifiers, or the
workflow summary.

A mode may also apply by itself. Give it `workflows`, `steps`, or both, in the
same shape as a hook's filters (`"*"` or a list of names), and it applies
automatically to every step they admit, in addition to the selected modes:

```yaml
modes:
  - name: tdd
    description: Write the failing test first.
    workflows: [task, bugfix]
    steps: [develop]
```

Here every `develop` step of `task` and `bugfix` (and of workflows inheriting
them) works in `tdd`, whatever `--mode` says: `--mode` replaces only the
workflow's default modes. A mode without either key applies only when
selected. `[]` on a mode means all, as on a hook. `discover` marks automatic
modes with where they are always on. The modes of each step are fixed when
the run starts; a later change to the configuration does not alter a running
task's pages.

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

## Manager and worker assignments

Caller role describes responsibility for one command. It is explicit workflow
coordination rather than authentication. The manager owns `start`, `next`, and
recovery. A worker may inspect status and complete or fail active work. Scripts and
direct service callers may omit the role; every execution command ww prints
includes one.

One manager `next` dispatches a structural assignment. For a leaf step, that
assignment contains its preparation hooks, main action, and completion hooks.
Each worker completion records one agent result, runs eligible automatic work,
and activates the next agent-owned workflow hook in the same assignment. The response exposes
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
an earlier one. The first instruction that asks for work shows the requirements saved by
`init` in full under "Task requirements", so the user's wording reaches the worker
without the manager adding commentary; later pages carry a one-line pointer to
`ww requirements <task>`, which prints them again (JSON pages keep
`task_requirements` and add `requirements_in_full` and `requirements_command`).
A paragraph the work instruction already quotes verbatim is shown there only.
`ww amend <task> --requirements "<text>" [--role ROLE]` appends a short
(at most 1000 characters), timestamped amendment recording who made it (the
caller role, or `operator`) and never rewrites the original; every page lists the
amendments, newest last, under "Task requirements" (JSON: `task_amendments`), and
`ww requirements` prints them after the original. A completed task refuses an
amendment. Each page also states that ww writes the artifact
from the completion command, never the worker under `.ww`. It then shows, under
"Previous step result", the handover of the step completed most recently
before this one. Completing an ordinary step requires
`--summary`, one or two sentences on what was done and what the
next step must know, at most 500 characters (a longer one is refused); ww stores it on the step record and shows it to the next
step together with the artifact's path, so the full result stays in the
artifact and is read only when the summary is not enough. Hooks, `init`,
and the built-in summary do not take one. Only ordinary steps count: hook results,
the built-in summary, and `init` are never chosen, and the history of earlier
loop rounds is included, so the first step of a later round sees the previous
round's last step. `artifact_from` remains the way to point a step at a specific
earlier artifact when the immediately previous one is not the right input.

An active step's Markdown instruction also names its later sibling steps. This
gives the worker a lightweight scope boundary without repeating those steps'
descriptions or prescribing a strict prohibition:

```markdown
### Next steps

Leave to them the work they cover:

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
persisted state after a restart. Caller roles do not change the concurrency
guarantees; a stale worker is stopped by its [assignment
token](#assignment-tokens).

### Assignment tokens

In the `auto` runtime every assignment the manager hands out carries a short
token. The bootstrap command gives it to the worker, and every command a
worker page prints repeats it:

```console
./ww instruction TASK-7 --run 01-task --role worker --assignment 3f9a1c07
./ww complete TASK-7 --role worker --assignment 3f9a1c07 --artifact="..." --summary="..."
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
replaced. A step the manager performs itself, `role: manager` or interactive,
is an assignment of its own with its own token, which no worker page ever
shows. Its page gives a manager completion command, `complete <task> --role
manager`, and ww refuses `complete` or `loop` with `--role worker` on it ("this
step is the manager's"), even with the step's token. The manager keeps every
override: it may still complete or recover any other step.

When the step after a manager's `complete --role manager` is also the manager's
own (`role: manager`, so no worker is selected), `complete` performs the dispatch
that `next --role manager` would and prints that step's work page under a
one-line note, so the separate `next` is unnecessary. It never does so when a
worker must be selected, when the task stops or waits for the operator, for an
assessment's outcome choice, at a loop boundary or a child coordinator, or for
`--role worker`; `complete --no-dispatch` keeps the pending page.

Tokens guard against a confused agent, not a hostile one: a worker that runs
the manager's commands is still not stopped. The `single` runtime, where one
session does every step, uses no tokens.

A handler that failed inside an assignment keeps the assignment open. When
`next --retry` asks again for the values the handler takes, the completion
command on that page carries the open token, so the worker supplies them with
the assignment it holds.

### Handoff to manager

In the `auto` runtime the manager does not take a worker's word for what
happened. When a worker's command ends its assignment (its last `complete` or
`loop`, a `fail`, or a `dispute`), the page ends with a "Handoff to manager"
block that ww writes from the saved state:

```text
Handoff to manager · assignment 3f9a1c07

Steps:
- review: completed
  artifact: /repo/.ww/tasks/TASK-7/runs/01-task/steps/03-review.md
- fix: completed
  artifact: /repo/.ww/tasks/TASK-7/runs/01-task/steps/04-fix.md
  checks: develop/sh passed
  fix rounds: 1

Files changed:
- src/app.py

Worker summary: Both findings fixed; the parser test covers the second.

Manager: continue with `./ww next TASK-7 --role manager`
```

It lists every agent item the worker performed in the assignment with its
outcome (`completed`, `loop break`, `loop continue`, `held for verification`,
`failed` with the error, or `not completed`), each artifact's path, the checks
of the last attempt with their status, the checks the operator waived, and the
number of fix rounds. "Files changed" is the change set since the first step of
the assignment with rules or checks began; without such a step, or without git,
it reads "not tracked". A failed handler's error is shown too. The worker's
own judgment reaches the manager only through its `--summary`.
The worker page tells the worker to return the block verbatim as its final
message and nothing else, and prints no further command for it. The manager's
bootstrap page names the block by the assignment's token, so the manager knows
which block answers which assignment. The JSON instruction carries the same
facts in `handoff_block`. The `single` runtime has no block.

### Confirmations

`next --retry`, `next --force`, a `next --replan` that reruns finished steps,
`rules prune`, `rules revoke`, `rules convert` and `rules decline` ask the
operator to confirm, because they can repeat an external effect, skip work,
record a command that will run from then on, or change the shared
rule-automation store. ww asks only at a terminal. An
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
| `handler_interrupted` | An automatic handler was interrupted and its outcome is unknown. |
| `loop_limit` | A loop reached its round limit: its `max_rounds`, else `limits.rounds`. |
| `fix_limit` | A step's check failed as many times as its rule's `max_fixes`, else `limits.fixes`, allows; see [Rules and checks](#rules-and-checks). |
| `check_disputed` | A step's worker disputed a check that rejected its completion; see [Checking early and disputing a check](#checking-early-and-disputing-a-check). |
| `value_unavailable` | An agent step reads a `{{ww.<namespace>.<name>}}` value its extension cannot give for the task yet, such as `{{ww.git.branch}}` before the task has a branch; the step has not started. `next --retry` checks again, `next --force` skips it. |
| `pass_incomplete` | An `items` pass finished its stages, but some items lack what those stages declare, such as an analysis or `reported`; see [Several passes over the same items](#several-passes-over-the-same-items). The next step has not started. Record the missing values with `update-item`, then `next --retry` checks again; the gate cannot be forced. |
| `plan_changed` | The workflow's definition changed since the run's plan was saved; see [When the workflow changes mid-run](#when-the-workflow-changes-mid-run). |

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
`next --force --reason`, are still shown, as the operator's choices; the
agent runs one only after the operator picks it. A delegated worker is not told
to ask anyone: it returns to its manager, and the manager asks.

The operator is someone ww waits for, not a caller: `--role` still accepts only
`manager` and `worker`, and `--role operator` is rejected.

### When the workflow changes mid-run

A run works from the plan it compiled when it started. When the workflow's
definition changes afterwards, say a hook's command was wrong and stopped the
task, and the operator fixed it in `ww.yaml`, the manager's next `next` (and
its `instruction` page) compiles the workflow again and compares it with the
saved plan, step by step and hook by hook. If anything differs, ww stops
before doing anything else, with `operator_reason: plan_changed`, and lists
each changed, added or removed step or hook, with the fields that changed,
before and after. This comes ahead of `--retry` too, so the fixed command is
offered instead of the old one being run again. The operator picks one of two
answers:

```console
./ww next TASK-1 --replan --role manager
./ww next TASK-1 --keep-plan --role manager
```

- `--replan` takes the new definition from the first changed item on. Items
  before it keep what they did; the first changed item and everything after
  it are the new ones, with fresh records, and `next` goes on from there.
  When the first change is in a step or hook that already finished, the run
  rewinds to it and runs it, and everything after it, again. The page names
  those steps, and `next --replan` asks the operator to confirm, or takes
  `--yes` once they have agreed. Earlier attempts move to the run's history,
  so their artifacts and output stay readable.
- `--keep-plan` carries on with the saved plan and is not asked again for
  the same configuration.

A change that leaves the run's own workflow as it is, such as a new workflow
beside it, is taken silently. A configuration that does not load stops
nothing: the run goes on with its saved plan, and `ww lint` shows the error.
Two changes cannot be applied to a running task, and the page says why,
offering only `--keep-plan`: a change to per-item or per-child stages that
the run has already expanded for its items or children, and a rewind past a
children step whose child tasks already exist. For those, reset the task and
start it again. A worker's pages never stop for a changed plan; the manager
decides, between assignments.

## Lock cleanup and command attempts

ww keeps lock sidecar paths in `.ww/locks`; a file there is not evidence of a
currently held lock. Remove inactive sidecars with:

```console
ww-agentic-workflows cleanup
```

Cleanup waits until active and waiting ww operations have left their lock gate,
then removes the old sidecars safely. The execution log records `started`
before a command runs and records its final `ok` or `error` result afterwards,
so an interrupted operation is visible as a `started` entry without a terminal
record.

## Projects: one ww instance over several repositories

Projects are optional. Without them a task works in the project root, where
`ww.yaml` and `.ww` live. With a `projects` list in
`ww.json`, that root can be a workspace directory above
several repositories, and each task or child chooses the repository it works
in. Projects live in the JSON settings rather than in `ww.yaml` because
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
`ww.local.json`; the list replaces the repo's whole, and its
relative paths still resolve against the root.

```console
ww-agentic-workflows start PROJ-123 --workflow feature --project backend ...
ww-agentic-workflows add-child EPIC-1 --text "Web part" --project frontend
```

The chosen project's directory becomes the task's working directory: commands
and hooks run there, `{{ww.task.workspace_dir}}` points at it, `{{ww.project.name}}`
holds the name, `{{ww.project.dir}}` the directory, and `{{ww.project.names}}` lists
every configured project name, joined by commas. `ww-agentic-workflows projects` prints the list as JSON.
Configuration, state, and artifacts stay in the root, so one
task's requirements, plan, and reviews are kept together even when its children
touch several repositories. `discover` lists the projects, and the children
collection step lists them so the agent can pass `--project` per child. A handoff
successor run keeps its project. A task without `--project` works in the
root.

The `ww/git` extension follows the working directory: branches, worktrees, and
commits act on the repository the task works in, and a task in a worktree still
resolves to that repository's primary checkout. A repository whose conventions
differ from the root's states them in its own settings file, described next.

### A project's own extension settings

A configured project may carry `ww.json` and
`ww.local.json` in its own directory. Of those files ww reads
exactly two keys, repo file then local file: the `extensions` section, applied
over the root's effective section for the same extension with the same rule as
between configuration levels (nested objects merge key by key, any other value
replaces the root's), and `task_format`, which replaces the root's for tasks
started in that project. A project therefore states only what differs:

```json
{
  "task_format": "WEB-{{digit}}",
  "extensions": {
    "ww/git": {
      "base_branches": {"default": "master"},
      "commit_format": "[{{ww.task.id}}] {{commit_message}}",
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
`frontend/ww.json (project 'frontend') configures unknown
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
ww.yaml is valid.
Configuration files: ww.yaml, ww.json
Project frontend extension settings: frontend/ww.json, frontend/ww.local.json
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
      - update-local-notes: Record the decisions in {{ww.task.workspace_dir}}/notes/.
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
root and `{{ww.task.workspace_dir}}` resolves to it; an `argv` or `shell` step
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
declares exactly the variable `task_id`, ww starts it as a short bootstrap request before the
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
        variables:
          - name: task_id
      - name: develop
        description: Implement {{task_id}}.
```

The bootstrap step must be the first declared flat workflow step and cannot have
hooks, nested work, or other variables. Its artifact is retained with the
task's run artifacts. If an ID is passed explicitly to `start`, it is
authoritative: ww supplies it as `{{task_id}}` and never accepts a replacement
from an agent.

### Children that bind their own IDs

The same step lets each child of a parent task obtain its own external ID, for
example one Jira story per child of an epic. When the child workflow's first
step declares the variable `task_id`, the parent's collection step tells the agent to record
children without `--id`; ww names each one by a temporary request ID until it
starts:

```console
ww-agentic-workflows add-child EPIC-1 --text "Story one" --project backend
ww-agentic-workflows start-child EPIC-1 REQUEST-20260923101500123456
ww-agentic-workflows next REQUEST-20260923101500123456 --role manager
ww-agentic-workflows complete REQUEST-20260923101500123456 --role worker \
  --variable task_id=PROJ-456 --artifact "Created PROJ-456."
```

`start-child` opens the identity request instead of a run, and the parent's
instruction points at it while it is in progress. Completing the request with
the tracker's key creates the child as `EPIC-1/PROJ-456` in its project
directory, with the identity step already done, and renames the parent's child
record. Each story is therefore created by its own step with its own retry and
failure handling: a crash halfway through the split cannot leave stories
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
clear it. A handoff workflow (one with a workflow transition) already starts
its successor and cannot recommend one.

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
`has_command`, `hook`, `check`, `interpretation`, and `missing`.

A rule without a check is judged after completion by a verifier, never by the
worker; the page says so. A rule whose wording already has a converted
derived check is listed with the checked ones, and a judged rule shows the
store's interpretation under it.

### The fix loop

`complete` runs the step's checks before recording anything. When one fails,
the completion is rejected: nothing is saved, the artifact is kept only as a
draft, the step stays in progress with its worker, and `complete` exits
non-zero. The response is the fix page, `## Fix required: 2 of 5 checks failed
(attempt 1 of 3)`, with each failed check's rule text, command, and the last 40
lines it printed, then the same completion command; `fix_required` in JSON.
The worker fixes the causes and completes again with a revised artifact.

A failed check always goes back to the worker, never to the operator, until
it has failed `max_fixes` times: the rule's own value, else `limits.fixes` in
`ww.json`, default 3. Then the task stops with
`operator_reason: fix_limit` and the last failures on the page. The operator
chooses:

- `next --retry` gives the worker another round: the count starts again, and
  the next `next` hands the step back.
- `next --force --reason "<why>"` waives the checks: the worker completes
  the step once more without them, and its artifact records the waiver.

Both ask for confirmation; `next --yes` confirms for an agent that carries out
what the operator said.

A hook without `on_failure: fix` fails like any handler, stopping the task with
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

A rule without a command is judged by another agent, a verifier, unless the
rule-automation store has a converted check for its wording: then ww runs
that check, for every step and task that has the rule. Verifiers only judge.
Building checks is the job of `ww-scriptize-rules`, a project task of its
own; see [Recording checks outside a task](#recording-checks-outside-a-task).

What ww knows lives in `ww-rule-automation.json` at the project root, a file
to commit, keyed by each rule's text hash; `checks` there are named, and one
check may cover many rules, the usual case for an ecosystem tool such as
deptrac, PHPStan, import-linter, ruff, or eslint, whose one configuration
expresses several rules. When a step begins, each of its rules without a
command is settled once: `converted`, checked by its store check where the
check's configuration files exist, or `judged`. A store written by an earlier
ww may still hold the interim entries verifiers once wrote inside tasks, such
as an approach or a proposed check; ww reads them, and their rules are judged.

When a step's worker completes and its checks pass, ww holds the completion:
nothing is recorded, the artifact is kept as a draft, and **verification
items** are inserted before the step, one per distinct worker the rules ask
for through `agent`, `model`, and `reasoning` (else the step's). Each is its
own assignment: under `auto` the manager hands it to a new worker; under
`single` the same session performs it, and its page says to read the change
as a reviewer would. The verification page lists each rule, the step's
changed files and the `git diff` that shows them, and the path of the held
artifact. The verifier gives each rule a verdict, `pass` or `fail` with
`file:line — what` evidence, as one `--rule-result` per rule,
`{"id": "<rule>", "status": "judged", "verdict": "pass"}`, and writes
nothing to the store. A failing verdict is a rejected completion: the step
goes back to its worker with the fix page, and it counts toward the rule's
`max_fixes` like a failed check. Once every rule passes, ww records the held
completion as submitted.

`discover --json` (`rules_notice`) and the first page of `start` say how many
declared rules have no check yet and suggest the `ww-scriptize` skill, which starts
`ww-scriptize-rules`. The notice never blocks a task; it is left out
while `ww-scriptize-rules` is switched off, and on the pages of
`ww-scriptize-rules` itself. `ww lint` warns with the IDs of those rules,
suggesting `ww-scriptize-rules` only while it is switched on, and
lists store entries whose wording no rule has any more; only
`ww rules prune`, after listing them and asking the operator, deletes the
orphans.

### Guiding the checks: `rules.check_guidance`

`rules.check_guidance` is free text, in the operator's own words, for whoever
builds a check, such as how the project runs its tools:

```json
"rules": {
  "check_guidance": "Development and quality checks run inside the docker container, on the worktree. Write every new check to run inside the container and act on the worktree, never on the host. Run a check on the host only when it uses nothing but the standard Linux tools, or when the host's tool versions, such as PHP, match the container's."
}
```

`ww rules --json` carries it as written, as `check_guidance`, and
`ww-scriptize-rules` follows it for every check it builds; it wins over ww's
defaults there. Like any setting, it can live in `ww.local.json` for the
operator alone. The `ww-rule` skill and ww's setup workflows honour it for
the checks they write, and `ww-suggest` proposes it when `project.md` records
a wrapper, such as a container, that checks must go through.

A project that wants no checks built leaves `ww-scriptize-rules` unused, or
switches it off with `"workflows": {"ww-scriptize-rules": {"enabled":
false}}`, which also silences the notice; its rules without a command are
judged on every completion. A check the team finds wrong can be undone at
any time:

```console
./ww rules revoke deptrac --reason "too slow for every step"
```

`rules revoke` shows the check, asks (`--yes` skips the question, and
without a terminal it refuses), and rejects the check and the rules it
covers, so they are judged by a verifier from then on. It changes only the
store: the YAML, the rule files, and the check's configuration files, such
as `deptrac.yaml` and an installed dev dependency, stay for you to keep or
remove.

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
- `next --force --reason "<why>"`: the check is waived for this step
  only; the worker completes again without it, and the artifact records the
  waiver.

A dispute changes nothing in the rule-automation store. Every dispute is also
kept in `.ww/rule-disputes.json`, and `ww lint` lists each disputed ID with
how often and where it was last disputed, since a check disputed again and
again deserves a look at its wording or command.

### Recording checks outside a task

A check is built once for the project and recorded outside any task's
verification, by `ww-scriptize-rules` or by hand:

```console
./ww rules convert phpstan --covers php/no-new-services php/typed-returns \
  --config phpstan.neon --proven \
  --check-argv -- vendor/bin/phpstan analyse --configuration phpstan.neon
./ww rules decline docs/tone --reason "A matter of review."
```

`--check-argv -- <arg>...` goes last: everything after `--` is the check's
argv, so the tool's own options, such as `--configuration` or `--select E`,
are not taken for ww's. Without `--`, `--check-argv` takes the arguments up
to the next option.

`ww-scriptize-rules` (the `ww-scriptize` skill) does this for every rule that
needs it, on a branch of its own. `rules convert` shows the check with its command in full, its config files,
the rules it covers with where each stands now, and anything else it
changes, and asks; `--yes` stands for the operator's answer,
and without a terminal it refuses. It creates the check, or replaces an
existing one's command, configuration and coverage; a rule it no longer
covers goes back to not scriptized. `rules decline` records rules as not
convertible: a verifier judges them, and `ww-scriptize-rules` leaves them out
from then on. Both
take `--dry-run`. `rules --json` gives each rule's `scriptize` state:
`command`, `converted`, `not_convertible`, `rejected` or `unscriptized`.

A converted check runs in a step only when all of its `config` files exist in
the directory the step's checks run in. A check built on a branch that is not
merged yet therefore does not run in another task's worktree: its rules are
judged there, and the step page, like the verifier page, says why, naming
the missing file.

### Reading the rules

`ww rule <task> <id>` prints one rule or check of a task as the task's plan
froze it: its full text, globs, rule file, command, and the steps that carry
it, and for a rule without a command what the store knows about its wording.
`ww rules` lists the project's declared groups, with their filters and each
rule's ID and first sentence, and each step's own rules; `--json` gives the
same for a program. `ww rules prune` deletes store entries no declared rule
needs any more, after listing them and asking; `--yes` skips the question.
`ww rules revoke <check>` rejects a converted or proposed check and its rules
the same way.

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
Added ww-rules.yaml to imports in ww.yaml.
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
  `ww.yaml`: the group goes into `ww-rules.yaml` next to
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
  looked up there. It refuses a check that is not converted, or that has a
  pending revision an earlier ww left, and a rule written in a step's own
  `rules` list.

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
the root `ww.json` `extensions`, even with an empty section;
a group name also declared in the YAML is an error.

`ww lint` ends with `Rules: N groups, M rules` when the configuration declares
any, after a notice for each absolute rule path.

## Workflow hooks, variables, and transitions

### Workflow hook scopes and ordering

These are workflow hooks, the lifecycle phases of `ww.yaml`;
the agent's own hooks, installed with `ww hook`, are
[agent hooks](#agent-hooks). Workflow hooks share the same handler syntax. Global hooks can filter with `workflows`
and, for step lifecycle phases, `steps`; workflow hooks can filter step lifecycle
phases with `steps`; step hooks cannot use either filter. The workflow boundary
positions are `before_start_workflow` and `before_complete_workflow`. They run
once in global → workflow order, cannot use `steps`, and cannot be declared at
step scope. Step lifecycle positions are `before_start`,
`before_complete`, and `after_complete`, and run in global → workflow → step
order for every matching step.
Step filters accept bare names at any nesting level or slash-separated logical
paths such as `plan-and-fix/fix` when only one substep should match.
Hooks, rule groups and automatic modes take one filter shape: `workflows` and `steps` are each
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
carry its own action selection. Put `handoff_to: "{{workflow}}"` directly on a
hook to declare a transition.

### Variables and metadata

Interpolations use `{{name}}`, with double braces everywhere. Every value ww
provides lives under `ww.`, so a name without it is always a variable a step
handed back: `{{ww.task.id}}` is the task's ID and `{{ww.task.workflows}}` the
ordered workflow-name list, joined by commas.
The core `{{ww.task.workspace_dir}}` variable is always the canonical directory for
the task: the project root by default, the configured project directory when a
task was started with `--project`, or the selected task checkout when an
extension such as `ww/git` supplies one. A step or handler with a
[`workdir`](#choosing-where-a-step-works) other than `task` reads the directory
it chose instead. `{{ww.project.name}}` is that project's name
and `{{ww.project.dir}}` its directory, both empty for a task in the root;
`{{ww.project.dir}}` keeps pointing at the project even after a worktree moves
the task workspace. `{{ww.project.names}}` lists every configured project name, joined
by commas. `{{ww.executable}}` is how the commands ww prints invoke ww, `./ww`
or the configured [`executable`](#choosing-the-ww-binary), so a step's text can
name a ww command as ww's own pages do: ``Run `{{ww.executable}} onboarding` ``.
Values under `{{ww.<namespace>.*}}` come from a configured extension, such
as [`ww/git`'s branch](#template-values-from-wwgit); a variable may not be
named `ww` or start with `ww.` (or `__`).
A handler's `variables` list declares what the step hands back, read later as
`{{name}}`. Each entry supports either `name` plus an optional `description`,
or the same compact `name: description` shorthand as handlers and steps, and
the performer passes it with `complete --variable name=<value>`. A bare string,
`- name`, is a value an automatic action returns itself. A step's values are
available to its completion hooks and later plan items.

When several automatic completion hooks request the same variable with the
same description, ww asks for it once and gives that value to each hook.
For example, project and workflow commit hooks share one `commit_message`.
Different descriptions for the same name are a conflict: the error names the
variable and both requesting plan items. A supplied `--variable` still appears
only once in the completion command.

```yaml
variables:
  - workflow: The workflow name corresponding to one of {{ww.task.workflows}}.
```

Use `saves` on an agent-owned handler or step to retain values. Each entry is a
prefixed path with an instruction: `metadata.<path>` keeps a value across
workflow runs of one task, `project_metadata.<path>` shares it with every task
in the project, `documents.<name>` updates a [document](#documents), and
`item.field.<name>` sets a [custom item field](#custom-item-fields). The path
after the prefix is the storage path, and the prefix is the scope:

```yaml
steps:
  - name: create-jira
    mcp: jira
    description: Create the issue and retain its ID.
    saves:
      - metadata.integrations.jira.issue_id: The ID of the created Jira issue.
  - name: inspect-jira
    description: Inspect {{ww.metadata.integrations.jira.issue_id}}.
```

The completion instruction includes every required value as a named argument,
its path written as in `saves` without the `metadata.` prefix:

```console
ww-agentic-workflows complete TASK-123 --role worker \
  --metadata integrations.jira.issue_id="PROJ-456" --artifact="<result>"
```

Shell and argv handlers automatically retain stdout when they declare
metadata entries in `saves`. For example, this handler finds an existing PR or
creates one, then stores its URL for later steps:

```yaml
handlers:
  - create-github-pr: ~
    shell: |-
      set -eu
      branch=$1
      base=$2
      repo=$(gh repo view --json nameWithOwner --jq .nameWithOwner)
      url=$(gh pr list --repo "$repo" --head "$branch" --base "$base" \
        --state open --json url --jq '.[0].url // empty')
      if [ -z "$url" ]; then
        url=$(gh pr create --repo "$repo" --head "$branch" --base "$base" --fill)
      fi
      printf '%s\n' "$url"
    args: ["{{ww.git.branch}}", "{{ww.git.base_branch}}"]
    saves:
      - metadata.github.pr_url: The pull request URL.
```

Later steps use `{{ww.metadata.github.pr_url}}`. ww saves the full stdout with
outer whitespace removed only after successful execution and assertions.
Send diagnostic messages to stderr to keep them out of the saved value.
`project_metadata.<path>` and `append: true` also work: an append entry receives
the whole output as one list value. Interrupted publication resumes from the
committed result without rerunning the command.

Metadata is task-scoped rather than workflow-scoped. `ww` stores it as a nested
object under `metadata` in `.ww/tasks/<task-id>/metadata.json`; a later run can use the
same `{{ww.metadata.<path>}}` reference. Metadata leaves are strings. A new value
may replace the same path, while a leaf/object path collision is rejected.
Inspect the complete object as JSON with `ww-agentic-workflows metadata TASK-123`.

A leaf declared with `append: true` is a list that grows across completions and
runs. Each completion passes the path once per value, or not at all, and ww
appends the values to what is stored, dropping repeats, without touching other
keys. The leaf interpolates as a comma-separated list, and as an empty string
before anything was saved, so a prompt never shows a raw placeholder. This is
how one task remembers the pull request threads it already handled across
several review passes:

```yaml
- process_pull_request: Create one work item per unresolved reviewer thread.
  items:
    report: Reply in the thread and resolve it.
    saves:
      - metadata.pull_request.handled_comments: The root comment id of the thread you just resolved.
        append: true
- get_pull_request_comments: >-
    Threads whose root comment id is in {{ww.metadata.pull_request.handled_comments}}
    are already handled; list them as needing no work.
```

```console
ww-agentic-workflows complete TASK-123 --role worker \
  --metadata pull_request.handled_comments=4711 \
  --metadata pull_request.handled_comments=4718 \
  --artifact="<result>" --summary="<handover>"
```

Under `items`, `saves` belongs to the built-in `handle-item` stage; with
configured stages, declare it on the stage that produces the value.

Use a `project_metadata.<path>` entry for values shared by every task in the
project. Project metadata has an explicit interpolation namespace so the
ownership of a value is visible where it is consumed:

```yaml
steps:
  - name: discover-environment
    description: Determine the shared staging URL.
    saves:
      - project_metadata.environments.staging.url: The staging environment URL.
  - name: deploy
    description: Deploy to {{ww.project_metadata.environments.staging.url}}.
```

The completion command still uses `--metadata`, with the path written as in
`saves`: `--metadata project_metadata.environments.staging.url=<value>`. Project metadata is stored as nested JSON in
`.ww/metadata.json` and can be inspected with
`ww-agentic-workflows metadata --project`. Project metadata is resolved live;
copy a value into task metadata when a task needs a stable snapshot. The `ww.`
namespace of project metadata is ww's own, holding its [onboarding
state](#onboarding-state); a `saves` entry under `project_metadata.ww.` is a
configuration error. Metadata
is plain runtime state and should not be used for secrets.

Every workflow also receives ww's built-in `update-workflow-summary` handler as
its final `before_complete_workflow` action. It asks the agent for a concise
goal/result summary, and ww writes that value to the task run ledger when the
run completes. No `ww.yaml` configuration is needed. Its instruction
lists every ordinary step's handover of this run in order, each with its
artifact, and tells the agent to build the summary from those alone, so the
summary cannot borrow counts or statuses from other runs or stale material. It
requests the run's ordinary worker selection (`auto`), unlike `init`, which
requests `cheapest` / `low`; `builtins.workflow_summary` in
`ww.json` overrides that.

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

The conversation comes first and is held in the session that can talk to the
operator. The agent presents, asks, listens, responds to questions and
corrections, and records nothing while they talk. Clear contextual completion,
such as "done", "I'm done", "looks good, continue", or an appropriate final
choice, lets the agent finish; if intent is ambiguous, it asks naturally
whether to continue or finish. "Done for today" can mean pause, leaving the
interaction open to resume later. One command records both sides, verbatim,
and ends the interaction:

```console
ww-agentic-workflows interact TASK-123 --role manager --transcript - --end <<'EOF'
Agent: Proposed: a listener dispatches a queued job per upload.
Operator: Resize through the queue; upload never waits.
Agent: Agreed: the upload returns before any image work starts.
EOF
```

`--transcript` reads a file, or stdin with `-`. A line starting with `Agent:`
or `Operator:` (also bold, as `**Operator:**`, in any case) starts an entry,
and the lines up to the next marker belong to it; text before the first
marker is refused. The entries keep their order and carry one recording time.
`--operator-said` and `--agent-said` record a single entry, and `--end` alone
ends an interaction whose entries are already recorded.

Every entry is appended to one file per task,
`.ww/tasks/<task-id>/interactions.md`, never rewritten, each headed by the time,
run, step, the item when the step is a per-item stage, and speaker, so the file
reads as the task's whole history of operator involvement;
`ww-agentic-workflows interactions TASK-123` prints it. The step's page shows
the conversation recorded for it so far.
Completing an interactive step is refused until at least one entry was recorded
and the interaction was ended. The step's artifact and handover are written as
usual; what goes into them is the agent's judgement.

For work where the operator wants to follow each action and edit, set
`explicit: true` on a workflow or step. The agent describes each meaningful
operation before it begins and shows the concrete edits for every changed file
afterward. Large edits can use a focused diff artifact; the agent still names
each changed file and redacts secrets. Structural groups, loops, item stages,
and child stages inherit the setting, and a nested step can turn it off with
`explicit: false`. The compiled plan preserves the resolved value across
resumption. WW-owned automatic handlers continue to show their command and
result through ww's existing output.

When the operator's answer is one of a few outcomes, declare them as `choices`
and ww turns them into a real pick rather than free text:

```yaml
items:
  analyze: Show the test case to the operator.
  interactive: true
  choices:
    - pass: The test case passed.
    - fail: The test case failed; no comment.
    - fail and give comment: The test case failed; the operator explains why.
    - skip: Skip this test case.
```

The page lists choices in order and tells the agent to use the host's native
choice tool when available, following its actual schema, and otherwise show a
numbered list in chat. For Codex, inspect the available question-tool schema
and use its supported structured options when offered; use a text-only
question only when required by that tool. Keep the pick pending until the
operator explicitly answers. A timeout, dismissal, or preselected value is not
an answer. The named tools for other
integrations include `AskUserQuestion` in Claude Code, `ask_user` in Gemini
CLI, `AskQuestion` in Cursor, `ask_question` in Antigravity, and
`ask_user_question` in Grok CLI. Kimi, DeepSeek, and custom agents get the
numbered list. The tool names come from the
[askmux](https://github.com/iShaldam/askmux) question-tool matrix (MIT,
Copyright (c) 2026 iShaldam) and the Gemini CLI documentation. The pick goes in the
same recording call, `interact --transcript - --choice "<label or number>"
--end`, a comment the operator adds is part of the transcript, and ending the
interaction is refused until a choice was recorded. The chosen label is kept on the step record and in the interactions
file.

The limitation to know: a delegated worker is a subagent and cannot talk to the
operator. An interactive step is therefore always performed by the session that
holds the conversation, the manager in the `auto` runtime: it is a
`role: manager` step, and the step's profile, agent, model, and reasoning
are ignored. For a manual-testing workflow, put `interactive: true` on the
per-item stage: the manager presents each test case, waits for the operator's
result, records it, ends the interaction, and completes the item.

When a session ends in the middle of a conversation, the `interrupt` hook
recovers what it can (see [Agent hooks](agent-hooks.md)). For Claude Code and
Codex it reads the session's own transcript file, keeps the operator's typed
messages and the agent's text replies since the step's attempt started, and
appends them to the interactions file under the speakers `operator (recovered)`
and `agent (recovered)`. The next session's notice says how many entries were
recovered, and the step's page shows them, so the agent continues from the
last unanswered point. The transcript formats are the agents' internal ones,
so recovery is best effort; for other agents the conversation is not recorded
and the notice says to ask the operator where they were.

### The operator page

A per-item stage declared with `interactive: page` is answered in the
browser, on an answer sheet over every item of the run, and ww applies the
answers itself. It is valid on one per-item stage per `items` step, because the page is shaped for items: other structures would
need a page of their own, and none exists yet.

```yaml
items:
  analyze: Show the test case to the operator.
  interactive: page
  choices:
    - pass: The test case passed.
    - fail: The test case failed; the operator explains why.
```

The page is an extra on top of the engine. The core knows it by
`interactive: page` alone: the stage's page tells the agent to run one command, and
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

Applying walks the plan in order from the current stage. For every
`interactive: page` stage whose item has an answer on the sheet, ww records the pick and the
comment and ends the interaction with `interact`, writes the answer onto
the item with `update-item` as its `actual_solution` (the pick, then the
comment after a colon) and marks it resolved for a `handle-item` or
`item_phase: resolve` stage and reported for a `handle-item` or
`item_phase: report` stage, completes the stage with `complete` and an artifact written from the
answer, and only then takes the answer off the sheet, so a wait that is
killed mid-way leaves at most a stale entry that the next wait drops. A
document the stage promised to update is the one thing ww cannot write: the
printed result names such documents and tells the agent to record the
answers of the applied items in them first. When the operator closes the
page, the result tells the agent not to open it again on its own. The walk stops at the first stage whose item has no answer, at any
stage that is not an `interactive: page` stage, and wherever ww needs the agent. Items are
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
operator's own words lift a pause: an answer on the page or an
operator entry, whether from a transcript, `--operator-said` or `--choice`, not the
agent's words and not the ending of a step.

## Documents

Metadata holds small values that steer a workflow. A document is the durable,
free-format counterpart: a file a workflow builds up and returns to across
runs, such as the test cases derived from an issue that a second run refines
after the issue changed and a `build-test-report` workflow reads later.
Declare documents once at the root; a task-scoped document lives under the
task directory, a `scope: project` one under `.ww/documents`, and a `scope:
user` one in the user configuration directory, shared by every project of the
user, unless `path` places it elsewhere in the
project (for a user document, elsewhere in the user directory), for example
`documentation/issues/{{ww.task.id}}/notes.md`, which then resolves inside the
task's worktree when the run has one:

```yaml
documents:
  - test_cases: The test cases derived from the issue, kept current across runs.
workflows:
  - name: derive-tests
    steps:
      - derive: Analyze the issue and derive test cases.
        saves:
          - documents.test_cases: One checklist item per case; keep items that still hold.
  - name: build-test-report
    steps:
      - report: Build the report from {{ww.documents.test_cases}}.
```

A step whose `saves` names a `documents.<name>` entry sees a "Documents to update" section
naming each document, its absolute path, whether it exists yet, and the
instruction. Its worker edits the file in place, the one exception to the rule
against writing under `.ww`. On completion ww checks that each promised file
exists and journals who updated it, run, step, time, and content hash, in the
task's `documents.json` or the project's. `{{ww.documents.<name>}}` resolves to the
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
its body-step artifacts. `artifact_from` names an earlier artifact-producing step
and adds that artifact to the later step's instruction; execution still
follows the written step order.

A nested step, in a group, a loop body, an assessment outcome, or a per-item
stage, may also name an earlier step of any enclosing level. The nearest match
wins: an earlier sibling first, then an earlier step of the parent's level,
and so on up to the workflow's top-level steps and `init`. An outcome may name
its assessment and a per-item stage its `items` step, whose work has finished
by then, and gets that step's own artifact; a loop body cannot name its own
running loop. The instruction names the dependency by its step path, such as
`review/check`, which is how `ww artifacts` lists it.

`artifact_from` may also name a plain group, which saves no artifact of its
own, or an assessment from a step after it. The later step then gets the
artifact of the latest step inside it that saved one in its current round:
for an assessment, a step of the outcome that was chosen. Loop rounds,
per-item stages, and per-child stages inside count, so the latest round's
artifact wins, and a loop wrapper that saves its own result counts when it
completes. A container that is itself inside a loop counts only the current
iteration of that loop: an artifact an earlier round saved is never handed
over. The instruction names that step and gives the artifact's file path.
When the chosen outcome saved nothing, for example an outcome with no steps of
its own, an assessment supplies its own artifact if it saved one. Otherwise
the instruction
says that no artifact is available and the step goes on without one.
Validation accepts such a dependency only when some path inside it can save an
artifact; an assessment whose outcomes cannot supplies its own answer.

```yaml
handlers:
  - name: analyse
    steps:
      - assess:
          question: Is the cause clear?
          outcomes:
            positive: {handler: investigate}
            negative: {handler: research}
workflows:
  - name: medium-task
    steps:
      - name: analysis
        handler: analyse
      - name: develop
        description: Implement the change.
        artifact_from: analysis   # investigate's or research's artifact
```

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
        artifact_from: research
      - name: review-and-fix
        loop:
          - name: fix
            description: Fix what the review found.
            artifact_from: research
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
stages. Under `items`, `analyze`, `resolve`, and `report` are
strings appended to the `handle-item` prompt as "When analyzing it", "When
resolving it", and "When reporting the outcome"; the item is still handled in
one pass with one artifact:

```yaml
- process_pull_request: Split the work by comment and sub-comment.
  items:
    assignment: together
    report: Reply in the same Bitbucket comment thread, then resolve the thread.
```

To control the stages, list them under `items.steps`. Mark stages with
`item_phase: analyze`, `item_phase: resolve`, or `item_phase: report` when they
update those standard item fields. `item_phase` is valid only on an acting
step inside a per-item stage (nested loops, groups, and assessment outcomes
count). On an `assess` step itself, or on a step outside any per-item stage, it
would do nothing, so validation and `lint` reject it: put it on the outcome
steps that do the work. `steps: []` collects items without processing them, for
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
              item_phase: analyze
            - fix: Resolve this comment.
              item_phase: resolve
              model: opus
            - reply: Report the outcome.
              item_phase: report
```

Worker settings cascade from the `items` step, to `items`, to each stage. The
step's `agent`, `model`, `reasoning`, `profile`, and `role` apply to
collection and are inherited by the stages; the same keys under `items` apply
only to the stages, and a stage's own value wins.

### One worker per item or for all items

In the `auto` runtime, `assignment` decides how many manager-dispatched
assignments the per-item stages become. By default one worker keeps going
through every stage of every item:

```yaml
- review: Review the change and record each finding as an item.
  items:
    assignment: per_item   # or together
    model: sonnet
    steps:
      - analyze: Analyze this finding.
        item_phase: analyze
      - fix: Resolve this finding.
        item_phase: resolve
      - reply: Report the outcome.
        item_phase: report
```

- `together`, the default, keeps one worker for every stage of every item.
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

### Several passes over the same items

A workflow has one item collection, and each `items` step is a pass over it.
Analysis and fixes that are cheaper together can run once between passes,
while every source comment is still checked and reported on its own:

```yaml
- collect: Record one item per comment with its stable source ID.
  items:
    steps: []
- analyze-together: Analyze all collected comments together.
- confirm-analysis: Reuse the collected items.
  items:
    steps:
      - analyze: Reuse the shared analysis; confirm and fill gaps.
        item_phase: analyze
- fix-together: Implement and verify the fixes for all analyzed items.
- finish: Reuse the collected items.
  items:
    steps:
      - verify-resolution: Verify the result and record actual_solution.
        item_phase: resolve
      - report: Report the result for this original comment.
        item_phase: report
```

Each pass expands its own stages when its collection step completes, for the
items recorded by then; an item added later joins the next pass. Passes share
the items' records, so the analysis written by the batch step is there for the
quick analyze checkpoints, and nothing an earlier pass recorded is cleared.
Inside a loop a pass is expanded again every round.

A pass with `steps: []` only collects or reconciles; `items: ~` stays the
shorthand for a single pass with the whole built-in lifecycle.

Leaving a pass requires only what its stages declare: an analyze stage needs
the item's analysis, a resolve stage its actual solution and `resolved`, a
report stage `reported`, and the built-in `handle-item` stage both `resolved`
and `reported`. So an analysis-only pass can lead into a batch fix, and a
workflow that only analyzes can end there. A linked comment reuses its
canonical item's analysis and fix but is reported itself. When a pass ends
with an `assess` stage, the check waits for the answer: the chosen outcome's
work is part of the pass, and an outcome that stops the workflow ends the run.
A pass whose items still lack something stops for the operator with
`operator_reason: pass_incomplete`, naming each item and what it lacks; record
it with `update-item` and run `next --retry`. `next --force` is refused at a
pass gate; the missing values must be recorded. An `items` step nested inside
another's per-item stages is rejected.

Automatic saves into item fields belong to per-item stages. A command on the
collecting `items` step itself cannot save item fields (one output cannot be
distributed among several items), and that is rejected; every field a stage
declares receives the same whole trimmed output. A report stage in which ww runs
nothing, one done by the agent alone, still marks its item reported with
`update-item --reported=true`.

`persistent`, `identity`, and `unique` describe the one collection, so the
first `items` step decides them. Later passes leave them out or repeat the
same values; a later pass that sets a different value fails `lint` and names
both steps.

### Items that persist across runs

Some item lists are the task's, not one run's: the test cases of a manual
test plan are the same in round one and round two. Declare the flow
`persistent` and the items outlive the run:

```yaml
- collect: >-
    Read the test cases in docs/test-cases.md. Compare them with the stored
    items and make the items match: add missing cases, remove cases that
    are gone, reword cases that changed. Keep the item ID equal to the
    case's heading.
  items:
    persistent: true
    interactive: page
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
and `update-item --text` to reword one. Removing and rewording are allowed only while the
collection step is in progress, and an item that other items refer to
cannot be removed. Stable IDs matter: an item that changed is reworded under
its ID, not replaced, so its history lines up across rounds. When nothing
changed, the agent completes the step as it is. With nothing stored yet, the
same page reads as a plain split.

```console
ww-agentic-workflows remove-item TASK-123 --id case-7
ww-agentic-workflows update-item TASK-123 --id case-3 --text "Upload a 25 MB image."
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
ww-agentic-workflows add-item TASK-123 --id c1 --text "Rename it." --field bitbucket_comment_id=100
ww-agentic-workflows update-item TASK-123 --id c1 --field bitbucket_reply_id=200 --field bitbucket_thread_resolved=true
ww-agentic-workflows item TASK-123 --by bitbucket_reply_id=200
```

A step declares the fields it sets as `item.field.<name>` entries of `saves`,
beside its metadata and document entries, and ww refuses to complete the step
while any of them is empty on the step's item, or on every item when the
step is the collection. Such a save needs an item: it belongs on an `items`
step or on a step, hook, or reused handler that runs within a per-item stage.
`lint` rejects one on an ordinary step, such as a batch fix between passes,
which updates items with `update-item` instead:

```yaml
- reply: Reply in the Bitbucket thread, then resolve it.
  item_phase: report
  saves:
    - item.field.bitbucket_reply_id: The ID of the reply you posted.
    - item.field.bitbucket_thread_resolved: Set to true once the thread is resolved.
```

Per-item stage prompts can read the stage's own item: `{{ww.item.id}}`,
`{{ww.item.text}}`, `{{ww.item.field.<name>}}`, and the lifecycle values
`{{ww.item.processed_item}}`, `{{ww.item.proposed_solution}}`,
`{{ww.item.actual_solution}}`, `{{ww.item.resolved}}`, `{{ww.item.reported}}`,
and `{{ww.item.reference_to_id}}`. `resolved` and `reported` render as `true`
or `false`; every other value renders as the empty string while unset (a
`reference_to_id` is empty for an item that links to none). The specification's
[Item values](specification.md#item-values) is authoritative. The effective current
step's `{{ww.choices}}` value is a JSON array of configured choice labels in
order, or `[]` when there are none. Use it as guidance in prompts or provided-
variable descriptions; ww does not validate a supplied value against labels.

Two flow-level rules make deduplication a refusal rather than a hope:

```yaml
items:
  persistent: true
  identity: bitbucket_comment_id
  unique: [bitbucket_comment_id, bitbucket_reply_id]
```

`identity` names the field every new item must carry. `unique` is one pool
of values across the listed fields: a value may appear once over all items,
in the run and, when the flow is persistent, in the task's stored items, so a
reply posted in round one cannot become an item in round two, and no two
items can claim the same comment. `add-item` and `update-item` refuse a
duplicate and name the item that holds it. The collection page states both
rules and the stored list shows each item's fields, so reconciling is a
diff on real IDs.

During collection and processing, use the item commands to maintain the
structured records:

```console
ww-agentic-workflows add-item TASK-123 --id comment-1 \
  --text="The error path is not tested."
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
        max_rounds: 5
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
automatic actions fresh operation IDs, while retries within one round
retain their existing idempotency identity.

### One worker per loop round

In the `auto` runtime, `assignment` decides how the body of one round is
split into worker assignments, like `assignment` does for per-item
stages:

- `per_round`, the default, keeps consecutive body steps in one assignment
  while they resolve to the same agent, model, reasoning, and profile. The
  worker completes each step with its own command and receives the next step
  straight away. The repeat boundary always ends the assignment, so the
  manager still sees every round and still receives the limit escalation.
- `per_step` hands every body step back to the manager.

Body steps keep their own `profile`, `agent`, `model`, and `reasoning`
overrides under `per_round`; a step whose settings differ from the
worker's simply starts a new assignment. That is deliberate: a `code-reviewer`
review followed by a `developer` fix stays two workers, so the reviewer never
fixes its own findings, while a fix-until-green loop under one profile runs as
one worker per round.

```yaml
- run-tests: ~
  assignment: per_round
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

ww limits loops to three rounds by default. Set a different project-wide
positive integer as `limits.rounds` in `ww.json`, or
override one wrapper with `max_rounds` in `ww.yaml`:

```json
{"limits": {"rounds": 4}, "extensions": {}}
```

```yaml
- review-and-fix: ~
  max_rounds: 7
  loop:
    - review: Review the implementation.
    - fix: Fix the findings.
```

The effective limit is saved in the compiled plan. When the completed round
count reaches it, ww does not begin another round or provide a continuation
command. The response reports `awaiting_operator` with `operator_reason:
loop_limit` and explicitly tells the manager to escalate the saved results and warning to the user for manual
resolution. It also shows the operator's one way past the limit: once the user
has resolved or accepted the remaining findings, `next --force --reason`
leaves the loop, records the reason on the repeat boundary, and continues with
the steps after the loop wrapper. Nothing else starts another round; a
further round needs a higher `max_rounds`.

## Parent and child tasks

A step with `children` splits a parent task into child tasks, then runs every
child with one workflow; the parent continues after the last child completes.
Child tasks live under the parent task directory and are intentionally limited
to one level for now.

```yaml
workflows:
  - name: feature
    steps:
      - name: split-work
        description: Split the feature into stories.
        children:
          description: One child per story.   # optional splitting guidance
          workflow: implementation

  - name: implementation
    steps:
      - name: implement
        description: Implement this child task.
```

While the step collects, record each child:

```console
ww-agentic-workflows add-child TASK-123 --text "Implement the API"
```

Without `--id`, ww uses the configured `task_format`, the child's project's
own when `--project` names one that sets it, else the root's, or the usual
generated `TASK-<timestamp>` ID when no format is configured. `{{timestamp}}`,
`{{digit}}`, and `{{uuid}}` are the supported placeholders. Supply `--id TASK-123.1` when you
want a stable, human-chosen child label instead. When the child workflow's first
step declares the variable `task_id`, omit `--id` and the child obtains its own ID from that
step; see [children that bind their own IDs](#children-that-bind-their-own-ids).
`--project <name>` runs the child in a configured project directory.

Once the step completes, the parent waits at `split-work/children`, the
item that runs the children; start a chosen child:

```console
ww-agentic-workflows start-child TASK-123 TASK-123.1
```

A child that has not started yet can still change its text or project (and,
at any time, its custom `--field` values):

```console
ww-agentic-workflows update-child TASK-123 TASK-123.1 --text "Implement the API and its client"
```

`update-child` works while the child is `pending`, during the collecting step
and while the parent waits, and refuses a child that has started, naming its
status. The child's first step records the text it has when it starts as its
requirements.

The child runs as `TASK-123/TASK-123.1`, with its own hooks, items, and run
history. The parent waits while a child is active. Completing the final child
automatically resumes and completes the parent's normal lifecycle; the
collecting step's completion hooks run after its children, and the parent's
later steps follow.

### Per-child parent stages

When the parent has its own work to do around each child, such as adjusting the
next slice to what earlier ones landed, reviewing the child's branch, and
merging it, give `children` a list of `steps` instead of a `workflow`. The
parent then runs those stages once per child, strictly one child at a time.
Exactly one stage carries `workflow:`; inside `children` that stage starts the
current child with that workflow and waits for it, it does not hand off (a
handoff is `handoff_to`, which cannot run inside `children.steps`). The stages
are assigned one per step (`children.assignment: per_step`, the only value
built; `per_child` is reserved).

```yaml
workflows:
  - name: roadmap
    steps:
      - read-plan: Add one child per slice of the plan, with the slice's full text.
        children:
          steps:
            - refine: Adjust {{ww.child.text}} to what earlier slices landed.
              role: manager
            - implement:
                workflow: task
            - review: Review {{ww.child.git.branch}} against the slice.
              artifact_from: implement
              role: manager
              break: The roadmap is done; nothing else is worth building.
            - land: Merge {{ww.child.git.branch}} into {{ww.git.branch}}.
      - close-plan: Record what the roadmap delivered.

  - name: task
    steps:
      - develop: Implement {{ww.task.id}}.
```

With two children `A` and `B`, the parent runs `refine`, `implement` (child `A`
runs its `task` workflow), `review`, and `land` for `A`, then the same four for
`B`, then `close-plan`. `ww plan --workflow roadmap` shows the stages under
`read-plan/{child}`; once the children are collected they become
`read-plan/child-1/refine`, `read-plan/child-2/refine`, and so on.

- A stage reads its child as `{{ww.child.id}}`, `{{ww.child.text}}`,
  `{{ww.child.project}}`, `{{ww.child.field.<name>}}`, and the child task's own
  extension values, such as `{{ww.child.git.branch}}` and
  `{{ww.child.git.base_branch}}`. Its page also names the current child and,
  while it has not started, how to change it. Stages before `implement` run
  before the child task exists, so they may read only its ID, text, project,
  and fields; reading `{{ww.child.git.branch}}` there is a configuration error.
  A value that is not available yet, such as a field the child does not carry,
  stops the task before the stage starts, for the operator to retry or skip;
  the error names the `update-child` command that sets a missing field.
- `refine` runs before the child starts, so it may rewrite it with
  `update-child TASK-123 A --text "..."`; the child's `init` records that text
  as its requirements.
- Children carry custom fields like items: `add-child ... --field area=parser`,
  and `update-child ... --field area=lexer` at any time.
- Add `start_child` beside `workflow:` to let ww start the child itself:
  `implement: {workflow: task, start_child: {model: "{{ww.child.field.model}}"}}`.
  It takes `workflow`, `runtime`, `model`, `reasoning` and `agent`, each a template
  over the child's record, read when the stage runs; an omitted or empty value
  inherits as `start-child` does. Record the settings with
  `update-child ... --field model=...` (or `add-child --field`) in the stages
  before. The manager's `next` starts the child and shows its page; a failed
  launch stops the stage for the operator, and `next --retry` starts it again.
- Without `start_child`, the `implement` stage shows `start-child TASK-123 A` for its own child only;
  its artifact is the child's workflow summary, which `review` reads through
  `artifact_from: implement`.
- In `auto`, the parent's manager also manages the child: starting it returns
  the child's page, the child's steps are ordinary worker assignments the same
  manager dispatches, and the completed child's page names the parent command
  to continue with.
- A child that fails stops the parent for the operator, as in the simple form.
- `break` on a stage ends the loop over the children: every remaining stage is
  skipped and each child that has not started is marked `skipped`. A `break`
  inside a `loop` within a stage ends that loop only; a `loop` inside the
  stages runs its own rounds for each child.
- A step with `children.steps` cannot sit inside a `loop`; the simple
  `children: {workflow: ...}` form can.

Steps may contain recursive `steps` without a depth limit. The plan remains
flat: each leaf action uses a hierarchical ID such as `parent/child`, with its
immediate `parent` recorded in JSON and shown in Markdown. Parent steps are
stateful grouping boundaries: they become in-progress with their first child and
complete when every child and parent completion hook completes.

A workflow such as `decide-on-workflow` hands off when it ends with a workflow
transition that starts a successor and never returns. The transition itself
makes it a handoff workflow; there is no flag to set. The transition is a step
with `handoff_to` beside its name, usually interpolating a variable an earlier
step handed back; the same key as the last `after_complete` hook of the last
step is equivalent. `workflow:` on a step is rejected naming `handoff_to`:
`workflow` only ever means "run a child with this workflow", under
`children`:

```yaml
- name: decide-on-workflow
  steps:
    - classify: Decide which workflow fits this request.
      artifact: false
      variables:
        - workflow: One of {{ww.task.workflows}}, other than decide-on-workflow.
    - route: ~
      handoff_to: "{{workflow}}"
```

Loading the configuration rejects more than one transition and a transition
anywhere but last: a transition step that is not the last top-level step, or
that a completion hook would follow; a transition hook on another step, in
another phase, or followed by another `after_complete` hook; and a transition
hook at global or workflow scope. The plan marks the workflow as a handoff. Execution
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
`ww.yaml` is loaded.

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
        kind: prompt
        hooks:
          before_start:
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

Extension settings live in `ww.json`, ww's project config file —
separate from `ww.yaml`, which describes what a workflow *does*:

```json
{
  "extensions": {
    "ww/git": {
      "commit_format": "{{ww.task.id}}: {{commit_message}}",
      "base_branches": {
        "default": "main",
        "bugfix": "develop",
        "task": {"argv": ["./scripts/base-branch", "{{ww.task.lane}}"]}
      },
      "separate_branch": true,
      "branch_name_formats": {
        "default": "feature/{{ww.task.id}}",
        "bugfix": "hotfix/{{ww.task.id}}"
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

The formats read `{{ww.task.id}}`, `{{ww.task.workflow}}`, `{{ww.task.lane}}`,
`{{ww.task.run}}`, and, in `commit_format`, `{{commit_message}}`.
`{{ww.task.lane}}` is the workflow whose branch handling the task takes: the
workflow's `hooks_from` when it has one, else the workflow itself, the name
`branch_name_formats` and `base_branches` are looked up by.

For `ww/git`, `ww-agentic-workflows extension ww/git settings` prints what actually resolved,
which is the first thing to run after editing the file; `--project <name>`
prints what a task in that configured project receives.

When `worktrees` is enabled, generated task IDs reserve any existing path
rendered by `worktree_dir` and `worktree_name_format`. For example, an existing
`worktrees/TASK-1` makes the next `{{digit}}`-formatted task use `TASK-2`, rather
than adopting that checkout for a new task. A generated ID also skips one
`ww/git` still holds: a branch record for it, or an existing branch its branch
name formats render for it, so a cleaned-up `.ww/tasks` does not hand an old
task's ID, and with it that task's history, to a new one.

`base_branches` maps exact workflow names to base branches, and its `default`
entry covers every other workflow, the same shape as `branch_name_formats`. A
`ww-scriptize-rules` task always uses the required `default` entry, even when
an entry names that workflow explicitly. A
repository under a configured project with a base branch of its own sets
`base_branches` in [its own settings file](#a-projects-own-extension-settings).
Each value may be either a literal branch name or an
object with a non-empty `argv` array. An argv command runs directly without a shell in
the project root; its single non-empty stdout line becomes the base branch.
Arguments may interpolate `{{ww.task.id}}`, `{{ww.task.workflow}}`, `{{ww.task.lane}}`, and `{{ww.task.run}}`;
a script choosing the base by workflow reads `{{ww.task.lane}}`, so a workflow with `hooks_from` gets its lane's base.
The resolved base is recorded with the task branch so retries, worktree creation,
and return-to-base use one stable value. The record is trusted only while it
names the branch being resolved and that branch exists; a record of another
branch, or of a deleted one, is left behind by an earlier task under the same
ID, and the configured base is resolved instead. A child task always uses its
recorded parent task branch instead. `reset` drops the task's branch and commit
records, and its children's.

`branch_name_formats` names the available branch naming strategies. Without an
override, `start-task-branch` first looks for a strategy matching the workflow
name and then falls back to `default`. Select another configured strategy for a
single run by name:

```console
ww-agentic-workflows start TASK-123 --workflow task --agent codex \
  --requirements="Fix the requested bug." --branch-strategy bugfix
```

The selection is persisted with the run, so later or retried branch and
worktree handlers use the same format. An unknown explicit strategy fails
instead of silently falling back.

`on_signing_failure` says what `git-commit` and `merge-branch` do when git
cannot sign a commit, for example because the signing agent is locked while the run goes on
unattended. `operator`, the default, stops for the operator as for any failed
handler. `unsigned` commits once more with `commit.gpgsign=false`, records
`signed: false` for the commit, and says so in the handler's result; any
other commit error still stops. Set it where the unattended run happens, such
as the user or local settings file:

```json
{ "extensions": { "ww/git": { "on_signing_failure": "unsigned" } } }
```

#### What `ww/git` does with those settings

| Handler | Settings it acts on |
| --- | --- |
| `git-commit` | `commit_format`, `on_signing_failure` |
| `merge-branch` | `commit_format`, `on_signing_failure` |
| `start-task-branch` | `base_branches`, `separate_branch`, `branch_name_formats`, `worktrees`, `worktree_dir`, `worktree_name_format` |
| `return-to-base-branch` | `base_branches`, `separate_branch` |
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

#### Landing a branch with `merge-branch`

`merge-branch` merges a branch into the task's current branch, in the task
workspace, with `git merge --no-ff`, so a "land" stage is ww's work instead of
an agent's. It takes two `args`, the branch to merge and the merge message;
both may use templates, and the message goes through `commit_format` like a
`git-commit` subject:

```yaml
children:
  steps:
    - implement:
        workflow: task
    - name: ext/ww/git/handlers:merge-branch
      args: ["{{ww.child.git.branch}}", "Land slice {{ww.child.id}}"]
```

It refuses a workspace with uncommitted changes, a branch that does not
exist, a detached `HEAD` (the merge would land on no branch), and a workspace
where a rebase, `git am`, cherry-pick or revert is in progress. On a conflict it runs `git merge --abort` and fails naming the
conflicting files, so the task stops for the operator with the workspace as
it was. When git cannot sign the merge commit it follows `on_signing_failure`
exactly as `git-commit` does: it stops, or, with `unsigned`, aborts the
half-made merge, merges once more without a signature, and records
`signed: false`. It reports success only when no merge is left in progress
(no `MERGE_HEAD`) and `HEAD` is a merge commit of the branch. The merge commit's
sha is its output, `{{merge_commit}}`, and the commit is recorded with the
merged branch; `commits` lists it. A branch already contained in the current
one merges nothing and succeeds with an empty `merge_commit`.

Like `git-commit`, it puts ww's operation ID in a `WW-Operation` trailer, so a
retry after an interruption finds the merge commit the earlier attempt made
instead of merging again. A merge that attempt left half-done (its own
trailer in `MERGE_MSG`) is aborted and redone only while it is untouched:
every conflicted file still carries its conflict markers, and nothing is
staged or changed beyond what git's merge left (the merged branch's version
of a file only it changed, or the clean three-way merge of one both sides
changed). Once the operator has worked on it, by resolving or staging a file,
the handler changes nothing and fails asking them to conclude the merge with
`git commit` (a retry then finds that commit by its trailer) or discard it
with `git merge --abort`. Any other merge in progress is refused.

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

#### Template values from `ww/git`

With `ww/git` in the settings (a section, even an empty one), every template
of every workflow may read the task's branch:

| Value | What it is |
| --- | --- |
| `{{ww.git.branch}}` | The task's branch. A child task's is `<parent branch>-<child ID>`. |
| `{{ww.git.base_branch}}` | The branch it was created from; a child's is its parent's branch. |
| `{{ww.git.branch_strategy}}` | The branch format key in use: `start --branch-strategy`, else the workflow's own `branch_name_formats` entry, else `default`. |

```yaml
- land: Merge {{ww.git.branch}} into {{ww.git.base_branch}} and push.
```

The branch values come from what `start-task-branch` or `create-worktree`
recorded for the task, never from a live `git rev-parse`: the primary
checkout and a task's worktree can be on different branches. Before the task
has a branch, an agent step that reads one does not start: the task stops
for the operator with `operator_reason: value_unavailable` and an error naming
the variable and its extension; `next --retry` checks again once the branch
exists, and `next --force` skips the step. An automatic handler that reads one
fails as any handler does (`handler_failed`). Wire `start-task-branch` before
the first step that uses them. An unknown `ww.`
name, or `{{ww.git.*}}` without `ww/git` in the settings, is an error when
the workflow is loaded.

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
    handlers=(ExtensionHandler("greet", _greet, "Say hello.", outputs=("greeting",)),),
)
```

For a packaged extension, name the `ww.extensions` entry point `acme.hello`;
the dot maps to the `acme/hello` identifier without importing the package.
Discovery is lazy, so unrelated workflows and saved-state commands do not load
the extension. An extension is trusted in-process Python once referenced:
qualified references isolate names, not side effects or process access.

The context identifies the current plan item, work item, attempt, and stable
operation. Human-readable `output` is kept on the execution record; structured
`values` must exactly match the `outputs` the handler declares (in YAML, a
handler returning values lists them as bare `variables` entries) and become
available to later workflow actions as `{{greeting}}`. Invalid return types,
undeclared values, and missing declared values fail the handler consistently.

A handler that needs settings per use declares `arguments`, the names of its
positional arguments in order. A reference passes them as `args`, a list of
strings in which templates are allowed; ww checks the count and the template
names when it compiles the workflow, renders the templates when the handler
runs, and hands the result over as `context.arguments`. A reference to a
handler that declares none may not pass `args`.

```python
ExtensionHandler("merge-branch", _merge, arguments=("branch", "message"))
```

A handler that declares `provide` (its agent-supplied inputs, the Python
counterpart of `variables`) may also declare `validate`, a callable that
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
    "git-commit",
    _commit,
    provide=(ProvidedVariable("commit_message"),),
    validate=_subject_error,
)
```

An extension that keeps records per task, as `ww/git` keeps branch and commit
records, may declare two optional hooks so a reused task ID inherits nothing.
`claims_task` receives a context with `task_id` (and `workflow` and the
project's `config`) and returns `True` while the extension still holds
anything for that ID; a generated task ID skips such an ID, as it skips one
whose `reserved_paths` exist. `forget_task` receives the same context and drops
the records of the task and its children; `reset` calls it. ww asks the
extensions the root or the task's project lists, even with an empty section,
and any that has a store.

```python
def _claims(context):
    return context.store.read_text(f"{context.task_id}.json") is not None


EXTENSION = Extension(
    vendor="acme",
    name="tickets",
    claims_task=_claims,
    forget_task=_forget,
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
`{{ww.task.workspace_dir}}` from the primary checkout or its recorded worktree.

New values go in the extension's one `namespace`, an `ExtensionNamespace`
whose `ExtensionVariable` entries templates read as
`{{ww.<namespace>.<name>}}`. A namespace is available whenever the root
settings list the extension, even with an empty section, and not only when
one of its handlers is referenced. Each value is resolved for the task each
time ww renders the task's templates; a resolver returning `None` means "not
available yet": an agent step that reads it stops the task for the operator
(`value_unavailable`) before it starts, and an automatic handler fails. The
namespace `ww` and the names ww keeps for its own values (`task`, `project`,
`documents`, `metadata`, `project_metadata`, `item`, `child`) cannot be
claimed, and two listed extensions claiming one namespace are an error.
`ww/git` declares `git`:

```python
namespace = (
    ExtensionNamespace(
        "git",
        (
            ExtensionVariable("branch", _branch_variable),
            ExtensionVariable("base_branch", _base_branch_variable),
        ),
    ),
)
```

`ww/git` is bundled with the installed `ww-agentic-workflows` package, so it is
available in every project, including a `pipx --editable` installation whose
source checkout lives elsewhere. A third-party extension remains project-local.
A project cannot provide a second `ww/git`; duplicate extension IDs are an
error. A handler is one unit of work: a retry re-runs the whole thing, so write
handlers that tolerate that.

## Execute a workflow

```console
ww-agentic-workflows start TASK-123 --workflow task --agent codex --runtime single \
  --requirements="Implement the requested change." \
  --model gpt-5 --reasoning high --role manager
ww-agentic-workflows next TASK-123 --model gpt-5 --reasoning high --role manager
# perform the displayed prompt, skill, or slash command
ww-agentic-workflows complete TASK-123 --role worker \
  --artifact "<whole result in Markdown>" \
  --summary "<one or two sentences for the next step>"
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
can be skipped with `next --force --reason "<reason>"`. This is an exceptional operator command:
ww asks for an interactive confirmation and explains that agents must obtain
permission before using it. Answer `no` (or provide no answer) to leave the
failed item in place; answer `yes` only after the operator has approved the
forceful transition.

```console
ww-agentic-workflows next TASK-123 --force --reason "Resolved manually" --role manager
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
is reported by `argparse` with exit code `2`. Abbreviated flags are not
accepted: every flag is spelled in full.

The task ID may be omitted from `start`. `task_format` in
`ww.json` then controls generation with `{{timestamp}}`,
`{{digit}}`, and/or `{{uuid}}`; without it, ww uses `TASK-{{timestamp}}`. It is a
setting of the checkout and of the tracker a repository uses, not of what a
workflow does, so it lives in the JSON settings, at any of their
[levels](#user-repo-and-local-configuration), and a configured project may
carry [its own](#a-projects-own-extension-settings). A `task_format` key in
any YAML file is an error that names the file and points here.

```json
{"task_format": "TASK-{{digit}}"}
```

Prefer an explicit ID whenever the request names an external ticket, so the
task matches the issue it works on; `discover` and the embedded agent
instructions say so. Avoid a generated format that imitates your tracker's keys,
such as `FOOBAR-{{digit}}` next to Jira's `FOOBAR-10859`. To rule generated IDs out,
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
`next --force --reason` to skip it, so the agent can run the one the
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
instruction record. Project-local `.ww/templates` files are not a supported
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
--force` with a specific `--reason`; this also requires confirmation and
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
Lock files live in `.ww/locks` and the kernel releases them when a process
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
task's saved state and artifacts, and the records extensions keep for it, such
as `ww/git`'s branch and commit records, and therefore requires explicit
confirmation.

```console
ww-agentic-workflows init
ww-agentic-workflows cleanup
ww-agentic-workflows reset TASK-123 --yes
```

## When an automatic handler fails

By default, ww stops the task and hands the decision to the operator. An
automatic shell/argv handler can instead declare `on_failure: fix` to request
an agent repair before ww retries it:

```yaml
- build: ~
  shell: npm run build
  on_failure: fix
  on_failure_instruction: Fix the build errors reported by the command.
```

ww runs the handler until it fails, then gives an agent a repair assignment
containing the command, diagnostics, full output references, and optional
failure instruction. The agent fixes the cause and submits `complete` with an
artifact; ww retries the handler and advances only on success. Completed
preceding handlers stay completed. The repair belongs to the failed execution
and creates no workflow step or hooks.

In `auto`, the manager dispatches the repair and repeated failures stay with
that worker; in `single`, the session receives it directly. Repair artifacts
survive reloads and are available through `artifacts`. Repeated failures reach
the existing `limits.fixes` operator stop (default 3). An operator retry renews
the budget, and an operator force skips the handler. Known failure retries
need no `idempotent: true`; unknown outcomes after interruption still use the
existing recovery rules.

`limits.auto_retries` in `ww.json` (default 0, never negative) makes ww retry a
failed automatic step that many times itself before any of this applies: an
interrupted step (unknown outcome) and a step that needs agent-supplied values
are never retried this way. Each failed attempt is kept on the step's record, and a
page that stops for the operator, or hands a repair to the agent, lists them
(`ww retried this step N time(s) itself`). An operator retry starts the count over.

Optional `on_failure_instruction` also adds guidance to hook failures.
`before_complete` hooks with `on_failure: fix` keep the step's existing check
loop with its worker; see [The fix loop](#the-fix-loop).

For the default `on_failure: operator` policy, the failure page hands the
decision to the operator as follows.

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
./ww next <task-id> --force --reason "<reason>" --role manager
```

`--retry` runs the same handler again, for when the cause has been fixed.
`--force` skips it and records the operator's reason in the task, so a skipped
check is visible afterwards rather than forgotten. A loop that hits its
round limit escalates the same way, and the force there leaves the loop.

## Installing the ww skills during init

`init` offers its bundled skills to every agent integration it knows about:
`ww`, `noww`, `ww-rule`, and `ww-setup` with the skills it guides through
(`ww-learn-project`, `ww-suggest`, `ww-refresh`, `ww-solve`,
`ww-rules-from-artifacts`, `ww-feedback-rules`, `ww-deduce-feedback`,
`ww-automate`, `ww-scriptize`, `ww-wizard`; see
[Setting ww up](#setting-ww-up-learning-and-suggestions)). In a terminal it
can redraw, that is one checklist rather than one question per agent:

```text
Install the ww skills (ww, noww, ww-rule, ww-setup, ww-learn-project, …) into which agent directories?
  ↑↓ move · space toggles · a all · enter confirms

 > [ ] .agents
   [ ] .codex
   [x] .claude       already present
   [ ] .gemini
```

Directories that already exist start ticked, because having one is good
evidence you use that agent. Nothing is written until you press enter, and
the answers are remembered in `.ww/init-choices.json`, so a later `init` only
asks about agents you have not decided on; `init --force` asks about every
agent whose `ww` skill is not installed yet.

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
installed later on its own. Without a recorded answer, a skill already
present in a chosen directory counts as accepted.

The rest of the summary adapts to repeat runs too. The box saying what to allow
in your agents' permissions is shown the first time only (and again under
`--force`), and the steps for getting started only while
`ww.yaml` defines no workflow. The documentation links and
the closing next step, the `ww-setup` skill, are always shown.

## Agent hooks

Agent hooks are the agent's own hooks (session-start, stop, interrupt),
installed with `ww hook` and separate from the workflow hooks above. They
carry ww's task state into a session — which task is unfinished, whether a
step was left mid-way — without blocking the agent. `agent_hooks` in
`ww.json` sets how many days back `session-start` looks for unfinished
tasks (`recent_days`, 3) or switches that scan off (`check_unfinished:
false`). See
[documentation/agent-hooks.md](agent-hooks.md) for the events, the
per-agent table, install/uninstall/show, failure behaviour, and interrupted
tasks.

## Choosing a runtime

A parent and its children can use different runtimes and session models. For
example, a Sol manager can coordinate an `auto` roadmap while each Luna session
handles a whole child in `single`:

```console
ww-agentic-workflows start-child TASK-123 TASK-123.1 \
  --workflow express --runtime single --model gpt-6-luna --reasoning high
```

Launch the child session with those actual host settings and give it the child's
manager instruction command; ww does not switch a running session's model.
The parent retains its own runtime/model/reasoning and resumes its review stages
after the child completes. Without flags, children inherit parent settings.
Changing only the model resets reasoning to `auto`; specify both for an exact
request. `--agent` starts the child for another agent and defaults to the parent's. Launch settings are fixed once starting begins and survive retries,
including external-ID bootstrap. This command cannot change an already-started
child's runtime or model. `--workflow` also overrides the child workflow named
by the parent coordinator, without changing the parent plan or earlier children.
The target must exist, cannot contain `children`, and must provide its own
first-step `task_id` variable when starting an external-ID request. The chosen
workflow survives interrupted starts and identity binding; it cannot change
after launch begins. Omit the flag to use the configured target.

`single` is the default, so an agent told little more than that would omit
`--runtime` and get `single` every time, including for workflows written to
delegate. `discover` therefore makes the choice explicit. Any workflow declaring an `agent`,
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

A project names the ww it runs in `ww.json`:

```json
{"executable": "ww-agentic-workflows-dev"}
```

The value is a command on `PATH` or a path. Every command ww prints for that
project starts with it — `ww-agentic-workflows-dev next TASK-1 --role
manager` — and the `./ww` launcher runs it, reading the key each time, so a
project switches installs by editing that one line. Like every setting, the key
may also come from the user or local settings file, the local one winning,
so one checkout can use a development install without changing the shared
file. Without the key, printed
commands use `./ww` and the launcher runs `ww-agentic-workflows`. `init` writes
`"executable": "ww-agentic-workflows"` when the key is missing. The launcher
itself is ww-owned: `init` rewrites a `./ww` that differs from the current one
and reports it as updated, because a launcher an older ww wrote can read old
configuration file names and run the wrong binary, and `lint` warns about such
a launcher, naming `init` as the fix. Choose the binary with `executable`, not
by editing `./ww`.

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
copy. In any other project, an `ext/ww/<name>` of its own is refused as
a duplicate of the bundled extension.

## Staying current with the ww checkout

ww is installed from a Git clone in editable mode, so whether a newer ww
exists is a local question. Before running the command it was asked for, ww
compares the checkout it runs from against the branch that checkout tracks,
and prints a short notice when it is behind:

````markdown
## A newer ww is available

This checkout is 23 commits behind `origin/main`.

What changed:

- `interact --pause` records that the operator is done for now.
- Items carry custom string fields, set with `--field NAME=VALUE`.

**Tell the person you are working for about this before you continue**, and let them decide whether to update. To update:

```console
git -C ~/tools/agentic-workflows pull
```

This notice is shown once. `ww updates` prints it again.
````

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
user, in `~/.config/ww/updates.json` (under `$XDG_CONFIG_HOME` when that is
set), because the
installation is shared by every project on the machine — acknowledging an
update in one project does not raise it again in the next.

```console
ww-agentic-workflows updates            # print the last notice again
ww-agentic-workflows updates --now    # look now, before the next check is due
```

For a command whose output is consumed by a program — the JSON catalogs,
`artifacts`, `items`, anything with `--json` — the notice goes to standard
error instead, so standard output stays parseable.

To switch the check off for a project, set `"update_check": false` in
`ww.json`. `WW_UPDATE_CHECK=0` switches it off everywhere,
and `WW_UPDATE_CHECK_INTERVAL` sets the seconds between checks.

## Learning from operator feedback

Feedback deduction is optional follow-up work after a workflow completes. It
adds no assignments, gates, handlers or steps to the workflow. Explicitly mark
artifact-producing steps that can contain useful negative feedback:

```yaml
workflows:
  - name: task
    steps:
      - review: Review the changes and record any corrections.
        learnable: true
      - confirm: Confirm acceptance with the operator.
        interactive: true
        learnable: true
```

`learnable` is a boolean, default `false`, independent of `interactive`.
Noninteractive reviews can be learnable; ordinary conversations are not
learnable unless explicitly marked. It requires `artifact: true`. The saved
plan retains this source metadata without inserting learning work. When an
eligible artifact exists and `feedback_learning` in `ww.json` is enabled
(default `true`), the completed-workflow page suggests the `ww-deduce-feedback`
skill. It does not run deduction automatically or postpone completion.

The skill reads negative feedback from eligible completed-step artifacts,
reasons about possible recurrence and matches existing generalizations by
meaning. It records whether enforcement could be scripted or needs reasoning,
including a concrete approach and its limits. A candidate is an observation,
not an obligation or an installed rule. A single encounter can be useful;
frequency is evidence rather than a required threshold or prediction.

The internal commands expose sources and stable point IDs:

```console
./ww feedback sources TASK-42 --run 01-task --json
./ww feedback --json
./ww feedback get feedback-a1b2c3d4e5f6 --json
```

`sources` returns artifact source IDs, completion timestamps and content through
ww's storage adapter. Only completed runs and explicitly learnable artifacts
are eligible, including retained loop-round results. `show` is an alias for
`sources`. Do not use arbitrary files or interactive transcripts as deduction
sources. Generalize feedback in the artifacts, not artifact headings or
instructions. The agent decides meaning; ww validates supporting quotes.

Write an analysis array such as this, substituting a source ID ww returned:

```json
[
  {
    "summary": "Use English variable names.",
    "reason": "Unspecified naming language can recur in new code.",
    "enforcement": "reasoning",
    "approach": "Review identifiers; dictionaries have false positives.",
    "evidence": [
      {"source": "artifact-a1b2c3d4e5f60000", "quote": "Use English names."}
    ]
  }
]
```

Then record the deductions after completion:

```console
./ww feedback record TASK-42 --run 01-task --analysis analysis.json --role manager
```

New points omit `id`. For another encounter of an existing point, supply its
exact `id` from `list` or `get`; ww rejects a same-wording update with new
evidence if the ID is omitted. Semantic matching of paraphrases belongs to the
agent. Repeating the same point and artifact quote does not add an occurrence,
including retries of new-point creation. New supporting quotes increment the
count; several quotes in one task still count as one distinct encountered task.
Record `[]` when no negative feedback generalizes. Recording deductions does
not change the completed run's state, plan, progress or assignment.

The locked, atomically written `.ww/feedback.json` store retains provenance,
occurrences, distinct encountered tasks, and `last_encountered_at`: the latest
supporting artifact completion time, not deduction time. This timestamp is
stored for later use; it does not affect pruning. `task_ratio` measures the
fraction of tracked tasks with encounters; `occurrence_ratio` divides supporting
quote encounters by tracked tasks and can exceed one. `completed_task_ratio`
uses completed tasks on both sides. Tracking counts each completed task once
and also includes explicitly analysed historical tasks; it never automatically
backfills old tasks. Earlier transcript-based points remain readable with their
IDs and counts; their timestamp is derived from the recorded interaction time.

Completion and deduction never delete points. Run `/ww-feedback-rules` for a
separate review of every candidate and a batch of concrete rule proposals.
The skill asks for explicit approval before writing rules through validated
`ww rules` commands. It also maintains the store using a separate command:

```console
./ww feedback prune --dry-run --json
./ww feedback prune --keep feedback-a1b2c3d4e5f6 --json
```

Pruning deletes candidates absent for five subsequently completed tasks. The
review skill examines all candidates first and retains stale points still
useful for proposals or deferred decisions with repeatable `--keep` IDs. A
pruning preview does not write anything. Original artifacts are never deleted.
No elapsed-time policy is implied by `last_encountered_at`.

Set `"feedback_learning": false` in `ww.json` to disable completion suggestions,
deduction recording and task-exposure tracking. Existing candidates and
artifacts remain readable. Pruning remains an explicit maintenance operation,
independent of that suggestion setting.
