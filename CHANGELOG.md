# Changelog

`main` is the branch users run, so everything recorded here is live the moment
it lands — there is no staging period and nothing is waiting to ship. Sections
are dated by the day `main` changed, newest first, because there are no version
tags yet to group them by. Pulled a week ago? Read down to that date and stop.

The package version stays at 0.1.0 while the release process is not yet in
place. What may change between two pulls, and what ww does not promise yet,
is in [documentation/limitations.md](documentation/limitations.md).

## 2026-09-30

- Rule commands: `ww check <task>` runs the active step's checks without completing or recording anything;
  `ww dispute <task> --rule <id> --reason "<why>"` asks the operator to overrule a check that rejected the
  completion (`operator_reason: check_disputed`; `next --retry` lets it stand, `next --force` waives it for that
  step); `ww rule <task> <id>` shows a rule in full; `ww rules` lists the groups and their rules, and `ww rules
  prune` deletes orphan store entries after asking. `next --yes` confirms `--retry`, `--force` or `--approve`
  without the prompt. After a verifier's completion records the held step under `auto`, the verifier's assignment
  ends and the manager dispatches what follows; `instruction --role worker` no longer offers a worker the
  manager's own `subagents: false` or interactive step. `ww lint` lists disputed checks, and the step page and the
  fix page name `check`, `rule` and `dispute`. `complete` alone exits non-zero on a fix page. Incompatible: plan
  schema 14 (rules keep their file) and execution state 9 (waivers per check, disputes), so finish or reset open
  tasks before updating. `f0ed6bc`
- Rules without a command are now judged after completion by a separate verification item, never by the step's
  worker: ww holds the completion, a verifier gives a verdict (a failing one is a fix round counted in `max_fixes`)
  or proposes how to check the rule, and after the operator approves the approach and then the prepared command
  (`operator_reason: check_proposed`; `next --approve`, `--approach`, `--pick`, or `--force`), the command is kept
  in `ww-rule-automation.json` at the project root and runs for that rule wording from then on. `complete` gains
  `--rule-result` and `--check-result` for verifiers; `ww lint` lists orphan and pending store entries.
  Incompatible: plan schema 13 and execution state 8, so finish or reset open tasks before updating. `7c40e11`

## 2026-09-29

- Rules: a root `rules` mapping of Markdown rule files and a step `rules` list deliver sentences to each agent
  step's page; a rule's `check`, and a `before_complete` hook with `on_failure: fix`, run when the step completes
  on the files it changed (`WW_STEP_CHANGED_FILES`, measured with git), and a failure sends the step back to its
  worker up to `max_fixes` times (`ww-agentic-workflows.json`, default 3) before `operator_reason: fix_limit`.
  `assert` gains `operator: empty`. Incompatible: plan schema 12 and execution state 7, so finish or reset open
  tasks before updating. `e39e2e4`

## 2026-09-28

- A ww install now also runs inside another checkout of ww's own source, such as a development install used in the `dev`
  checkout, instead of failing with "extension 'ww/git' is provided more than once". `4cb51aa`
- The stop hook now reminds a session only about its own agent's tasks or the task whose worktree it works in, never a manager about a step delegated to a worker; hook messages name a hook step by itself and its step. `7a004a2`
- `ww hook install --agent <agent>` (Claude Code, Codex, Cursor, Antigravity; `--local` for Claude Code) adds hooks that list unfinished tasks at session start, remind once about an open step, and mark interrupted sessions; see `ww interrupted`. `784f805`
- `task_format` moves from `ww-agentic-workflows.yaml` to `ww-agentic-workflows.json` (any level); a
  YAML `task_format` is now an error naming the file, `init` writes it into the JSON, and a configured
  project's own JSON may set the format for IDs generated with `--project`. `ecbbe17`
- A configured project's own `ww-agentic-workflows.json` and `.local.json` now supply its `extensions`
  settings over the root's for work done there; `ww/git` `project_base_branches` is gone, and `plan`,
  `extension`, `lint`, and `discover` show what a project gets. `191ef5b`
- Steps and handlers accept `workdir: task | project | root` to run their work in the task workspace
  (default), the `--project` checkout rather than its worktree, or the ww root. `3073b8d`
- `depends_on` in a loop body, group substep or per-item stage can now name an earlier
  artifact-producing step of an enclosing level, not only an earlier sibling. `d685543`
- When a task needs the user, ww now reports `control: awaiting_operator`, `next_role: operator` and an
  `operator_reason` in instruction and status output, instead of `blocked`. `07d34ec`
- A fresh `init` no longer writes a default workflow or `task_format` that a machine or local
  configuration file already provides. `6f30237`
- ww also reads `ww-agentic-workflows.machine.{yaml,json}` from `~/.config/ww-agentic-workflows/` and
  `ww-agentic-workflows.local.{yaml,json}` in the project; lower levels extend or override (`extends: false` opts out). `c39d4d0`
- `workflows.yaml` and `agentic-workflows.json` are now `ww-agentic-workflows.yaml` and
  `ww-agentic-workflows.json`; ww stops on the old names, and `init` renames them. `c65a0ef`
- `workflows.yaml` can import other YAML files listed under `imports`; later files
  override earlier ones, and `lint` notes each override. `6287457`
- A project can name its ww binary with `"executable"` in
  `agentic-workflows.json`; printed commands and `./ww` use it. `3c5f1a0`
- `--version` and the `init` welcome now say that ww is in beta, linking what
  is not promised yet. `cdda55d`
- Tasks saved by another ww version load again, and `init` offers a new skill
  once and repeats its setup notes only when they apply. `a381d2d`
- Plan and instruction JSON no longer carry the always-null `gate_prompt`
  field; the handler decision gate it held could not be configured. `690a69b`
- An assessment outcome can be `stop_workflow: true`, ending the run when
  chosen; before, only the compact form's `negative` could.
- An assessment with declared outcomes now accepts `positive`, `negative`, and
  `mixed` even when it does not declare them; an undeclared one runs nothing
  and continues, so a gate needs no placeholder branch.
- Assessment pages now list the outcomes and what each does, and the page
  after an assessment offers one `next --outcome` command per outcome instead
  of a plain `next` that ww refused. A repeated `next` after choosing no
  longer asks for the outcome again.
- A workflow can `inherit` another: it copies its steps, hooks, and settings,
  including global hooks filtered to it, and differs only by name, and so by
  its `ww/git` branch and base entries.
- `recommended_next_workflow` offers a workflow when a run completes; the
  agent asks the operator through its choice menu and starts it on the same
  task only on confirmation.
- `ww/git`: `base_branch` is replaced by a `default` entry in `base_branches`,
  as in `branch_name_formats`. A config still using `base_branch` is refused
  with a pointer to the new form.
- A hook that names a handler defining a `loop`, `steps`, or `items` is now
  rejected; it used to run as a one-word prompt without its loop.
- Every project now has a `catchall` workflow, provided by ww rather than
  `workflows.yaml`. It records a change to files that no workflow covers: one
  `work` step, performed by the session that received the prompt exactly as
  it would work without ww. `discover` lists it apart, with when to use it:
  only when files are about to change, never for questions or read-only
  work, and always through `lookup` below. The agent instructions and the
  `ww` skill say the same, and the skill now triggers before any change.
  Switch it off with
  `"workflows": {"catchall": {"enabled": false}}` in `agentic-workflows.json`,
  or replace it by defining a workflow of that name. A project whose
  `workflows.yaml` defines no workflow is no longer rejected.
- New `lookup [<task>] --agent <agent>`, the catch-all's entry point. It maps
  what the operator called the task onto the project's IDs (`12345` or
  `forms-12345` is `FORMS-12345` under `task_format: FORMS-{digit}`; with
  tracker keys, the one task ending in `-12345`) and answers with one next
  step: continue the task's unfinished run, start `catchall` on it, or ask
  the operator through the agent's choice menu, which a never-seen task, a
  reference matching several tasks, and a request naming none all require.
  It is read-only; the commands it prints run only after the operator picks.
- Operator choice menus now name the question tool of Gemini CLI
  (`ask_user`), Cursor (`AskQuestion`), Antigravity (`ask_question`), and
  Grok CLI (`ask_user_question`) besides Claude Code and Codex, and say to
  fall back to a numbered list where the tool is unavailable, such as Codex
  outside Plan mode. Tool names from the askmux matrix
  (https://github.com/iShaldam/askmux, MIT, Copyright (c) 2026 iShaldam).
- Starting a workflow on a task whose run of another workflow is unfinished
  is still refused, and the error now names the `instruction` command that
  continues the run; resetting is left to the operator.
- The task storage port gains an abstract `task_ids()`, listing top-level
  task IDs. A custom `TaskStorageAdapter` must implement it.
- `init --skills` installs a second skill, `noww`, next to `ww`. `/noww`
  tells the agent not to use ww for the rest of the conversation. Running
  `init` again asks once whether to add it to the directories you chose.
- A step with `subagents: false` under the `auto` runtime no longer shows the
  manager delegation text. The dispatch page says no worker is selected and
  shows a plain `next` instead of one with `--selected-agent None`, and the
  step page tells the manager to perform the step itself.
- Model and reasoning settings a workflow does not request are no longer
  shown to agents: no `Model: auto` lines in plans and pages, and no
  `--model auto --reasoning auto` in the suggested `next`. The `auto` runtime
  guidance now tells the manager to keep a worker's default settings instead
  of weighing a cost/quality trade-off of its own.

## 2026-09-27

- A value supplied with `--variable` is now checked by the handler that will
  consume it before the completion is saved. An extension handler may declare
  `validate` beside `provide`; `ww/git` does so for `commit_message`, so a
  multi-line subject now fails the `complete` command with
  `commit_message must be a single line` and records nothing, where it used
  to be accepted, fail inside `git-commit`, and leave the task for the
  operator. The extension API change is additive.
- Retrying a failed automatic handler that takes provided values no longer
  replays the values it failed with. `next --retry`, and a plain `next` on
  the failed task, ask for them again through the usual input request, which
  now lists what the handler was given last time, so a wrong value is
  corrected and a right one repeated. Before, the only way past a rejected
  value was `next --force`.
- A command handler can declare `idempotent: true`. When ww is interrupted
  while such a handler runs — the process died, the terminal closed, the
  machine went down — the next `next` replays the interrupted and unrun
  commands under the same operation identity instead of stopping at the
  recovery boundary, because the author has said a second run cannot do
  damage. The default stays `false`: an interrupted handler without the
  declaration keeps its unknown outcome until `next --retry`,
  `recover --mark-succeeded`, or a checker settles it. `lint` validates the
  key, the saved plan carries it, and `plan` shows it under **Recovery**.
- A command that exits non-zero is now recorded durably the moment it exits,
  not only when the whole handler's failure is written. A crash in that
  window used to leave the segment `in_progress`, so the next `next` reported
  an unknown outcome and asked the operator to decide about a command that
  had visibly finished; it now reports the known failure, with the exit code
  and what the command printed, and the ordinary `next --retry` applies.
- Task state was already written with `fsync` on the temporary file before
  the atomic rename and on the directory after it; a test now pins both, so
  the durability against power loss cannot regress silently.

## 2026-09-26

- A failed automatic handler now tells the operator enough to decide. The
  error names the command that failed and shows what it printed, falling back
  to stdout when stderr is empty — test runners, linters, and type checkers
  nearly all report on stdout, so the commonest failure in ww used to read
  `automatic handler failed (1):` and nothing more. Long output is tailed to
  its last 40 lines, with the whole of it still saved as an artifact.
- The escalation page now hands over explicitly rather than listing two
  commands. It tells the agent to report what failed and quote the output, to
  say that completed work is saved and nothing after the step has run, to ask
  for a decision without picking one, and to set the expectation either way:
  say when the cause is fixed and the handler is re-run, or ask for a skip in
  words with a reason to record. Then wait.
- `init` explains what `WW_AGENT_INSTRUCTIONS.md` is before asking whether to
  reference it, the way the task-ID question already described its options:
  that it tells agents to start from `./ww discover` and follow each response,
  that linking it from `AGENTS.md`, `CLAUDE.md`, or `GEMINI.md` means they pick
  it up without being told, and what answering no leaves you with.
- The operator page is marked experimental, in a badge beside its title and
  a note above the items, so nobody mistakes a page whose layout and answer
  handling may still change for a settled one.
- `init`'s permission tip is now a boxed `ACTION NEEDED` block above the next
  steps, and says why: ww runs the commands `workflows.yaml` configures, so an
  agent asks for confirmation every time until it is allowed — and an
  interactive step's operator page cannot open its local port from inside an
  agent sandbox at all. It also names what is actually being trusted, which is
  your own `workflows.yaml`. The block wraps to the terminal so the box never
  breaks.
- `init` asks far less and fits a normal terminal. Every agent directory is
  now settled on one checklist — arrow keys move, space toggles, `a` marks all,
  enter confirms — with the directories you already have ticked and labelled
  `already present`. That replaces nine near-identical yes/no questions, so a
  fresh Git project answers four prompts and one screen rather than fifteen
  prompts. A terminal that cannot be redrawn, such as a pipe or a captured
  stdin, still gets plain questions: one per existing directory and one shared
  question for the rest. No new dependency; ww still installs `PyYAML` alone.
  The welcome
  wordmark is 101 columns wide and used to shred itself on an 80-column
  terminal, so a narrower one gets a compact mark. The worktree directory
  prompt refuses a bare `y`/`n`, which it previously accepted as a directory
  name — creating a directory called `y` and recording it in the project
  configuration. "You're almost there!" is now "Next steps", one line after
  ww reports that setup completed.
- The compatibility wording is recalibrated to match reality. `workflows.yaml`
  and the command surface have settled, and most work on `main` is internal
  refactoring, new capabilities, and fixes — so the README says that, rather
  than warning that nothing survives a pull. What remains true is stated
  plainly: no formal guarantee until releases exist, incompatible changes
  deliberate and rare when they happen, and persisted task state the one
  genuinely volatile surface.
- `SECURITY.md` states what ww can do on the machine running it: that
  `workflows.yaml` is executable configuration, that the only request ww makes
  of its own accord is the update check's `git fetch`, that extensions run
  arbitrary code, and what `.ww/` can contain. CI now audits dependencies
  against known advisories on every run — over the frozen dependency set, so
  neither the editable project nor the auditor's own dependencies enter the
  result — and its actions are pinned to commit SHAs rather than movable tags.
- `discover` now advises which runtime to use instead of pointing at the
  default. A workflow that asks for an `agent`, `model`, `reasoning`, or
  `profile` on any step — at any nesting depth, including loop bodies, per-item
  stages, and assessment branches — is listed with the steps that ask and a
  note to start it with `--runtime auto`, because only `auto` delegates and so
  only `auto` can honour the request. The `--runtime` option no longer reads
  "omit it for the default", which was steering agents to `single` whatever the
  workflow wanted, and the JSON carries the same information as
  `delegation_requests` per workflow.
- ww tells you when the checkout it runs from is behind the branch that
  checkout tracks. The notice is written above the command's own output and
  names the changelog bullets added since, so an agent relaying that output
  shows the update to the operator before continuing; the command itself then
  runs untouched. The only network call is a `git fetch` against the remote
  the user cloned from, at most once a day, and every failure in it is
  contained. Each notice is shown once, recorded per user in
  `$XDG_CONFIG_HOME/ww-agentic-workflows/updates.json` rather than per
  project; `updates` prints the last one again and `updates --check` looks
  now. Commands whose output is parsed get the notice on standard error.
  `"update_check": false` in `agentic-workflows.json` turns it off for a
  project, `WW_UPDATE_CHECK=0` everywhere.
- A workflow may declare `restartable: true`: a new `start` while its
  previous run is unfinished abandons that run, which stays in the task's
  history with the new `abandoned` status, and opens a new one. Other
  unfinished runs still refuse a start.
- In the `single` runtime, `complete` and `loop` open the next agent step
  and print its page, running the `next` the same session would have run;
  `next` on a step that is already open shows it again instead of refusing.
  The `auto` runtime keeps the manager's `next`.
- Items carry custom string fields, set with `--field NAME=VALUE` on
  `add-item` and `update-item`, several per call, and found with
  `item --by NAME=VALUE`. A step declares the fields it sets with
  `update_item` and cannot complete while one is empty; per-item stage
  prompts read `{{item.id}}`, `{{item.text}}`, and `{{field.<name>}}`. An
  items step may declare `identity`, the field a new item must carry, and
  `unique`, a pool of fields whose values may each appear once across all
  items, including the shared store. The operator page renders Markdown,
  shows the fields, and records a pick or a comment as it is given; the
  wait settles for a moment after the last answer. A pick is shown only as
  ww recorded it, or as queued while no agent waits, never from the tab's
  own memory, and what the tab remembers belongs to one run. Choice questions in the
  session are asked in a line or two, with the matter presented first.
- `items: shared: true` keeps a task's items across its runs in
  `.ww/tasks/<task-id>/items.json`, refreshed as a run adds, changes, or
  resolves them. A new run starts from the stored items with outcomes cleared
  and its collection step reconciles them against the source, with the new
  `remove-item` command and `update-item --item`, both allowed during
  collection only. `start --fresh-items` forgets the store first.
- A per-item stage declared with `ui: true` is answered on the operator page,
  an answer sheet over every item of the run. `interact --await` serves the
  page for as long as it waits, then applies the answers through the ordinary
  `interact`, `update-item`, and `complete` commands, one stage at a time in
  plan order, and prints what it applied. Answers wait in
  `.ww/operator-ui/<task-id>.json`, outside the task's state, from the moment
  they are given. The page lives in the `operator_ui` package; the core knows
  it by the `ui` flag alone. In Claude Code the page tells the agent to run
  the wait in the background with a long `WW_OPERATOR_WAIT`, so the operator
  can keep talking to the agent while the page is open; elsewhere the wait
  blocks. The instruction now carries the task's `agent`. Applying writes
  the answer onto the item as its `actual_solution`, names the documents
  the applied stages promised so the agent records the answers there, and
  the work section of a `ui` stage points at the page first.
- `interact --pause` records that the operator is done for now; the step's
  page then tells the agent to stop until they return, and only the
  operator's own words lift it. Interaction entries of a per-item stage name
  their item, and a plain interactive step's page shows the conversation
  recorded so far.

- A workflow may declare `runtime`, used by `start` when `--runtime` is
  omitted; it outranks the project default and the flag outranks it.

- `choices` on an interactive step declare the outcomes the operator picks
  from; the page resolves them to the agent's own mechanism (`AskUserQuestion`
  in Claude Code, `request_user_input` in Codex, a numbered list elsewhere),
  `interact --choice` records the pick, and ending requires one.
- `interactive: true` marks a step as a conversation with the operator, held
  by the session that can talk to them (the manager in `auto`, like
  `subagents: false`). `interact` records both sides and ends the
  conversation, all entries append to one per-task `interactions.md` that
  `interactions` prints, and completion is refused until the conversation was
  recorded and ended.

- A step with no content of its own and a root handler of the same name now
  copies that handler, as a bare hook entry always did.
- Breaking: the `save_metadata` key is now `update_metadata`; the old key is
  rejected by `lint`.
- Root `documents` declare durable, free-format files, task-scoped or
  `scope: project`. A step's `update_document` names the documents it edits in
  place, `{{documents.<name>}}` resolves to the file's absolute path, ww checks
  the file exists on completion and journals who updated it, and
  `ww documents [TASK-ID]` lists them. A declaration's `path` places the file
  elsewhere in the project, `{task_id}` included, resolving inside the task's
  working directory when the run has one. `reset` removes the task's journal
  and its documents under `.ww`, like its metadata and interactions.

- Task working directories and project-local profile files are persisted
  relative to the project root and printed absolute for the current
  filesystem, so state written on the host is correct inside a container that
  mounts the checkout elsewhere.
- A pending-input page lists the handovers of the steps completed since the
  waiting handler last ran, and `ww/git` asks for a commit message about this
  round's work that never repeats an earlier commit's.
- One manager `next` now carries through preparation hooks before a loop and
  through nested loop entries to the first worker step, instead of stopping
  at each boundary for another `next`.
- `save_metadata` entries take `append: true`: the key holds a list, each
  completion may pass the name once per value or omit it, values are appended
  with repeats dropped, and the leaf interpolates as a comma-separated list or
  empty before anything was saved. `items` accepts `save_metadata` for the
  built-in `handle-item` stage.
- `ww/git`'s `git-commit` succeeds quietly when the workspace has no changes
  instead of failing the step; project commit hooks still do not run then.
- The built-in workflow summary lists every step's handover of the run, with
  artifacts, as its only inputs, and requests `auto` / `auto` instead of
  `cheapest` / `low`; `builtins.workflow_summary` still overrides it.
- A pending-input page whose body delegates to a worker now also carries
  the "delegate the assignment" heading instead of "provide required input".
- In the `auto` runtime the manager's delegate page and requested worker
  come from the assignment's step rather than a preparation hook at the
  cursor, both roles see every item the assignment covers, a continuing
  worker is told the same assignment continues, and its end says to stop.
- Completing an ordinary step requires `--summary-for-next-step`, a short
  handover stored on the step; the next step's instruction shows it under
  "Previous step result" with the artifact's path, and the full result stays
  in the artifact. Hooks, `init`, and the built-in summary are exempt.
- Every worker instruction repeats the requirements saved by `init` under
  "Task requirements" and states that results go through the completion
  command, never into files under `.ww`.
- An assignment with no agent step, only an automatic handler waiting for
  values, is no longer delegated: the manager supplies the values itself.
- `item_assignment` defaults to `all_items`, and stages that resolve to
  different worker settings, or set `subagents: false`, now start a new
  assignment instead of failing compilation, matching loop bodies.
- Under `items`, `process_item`, `resolve_item`, and `report_item` strings
  give phase guidance to the built-in `handle-item` stage, so a short-form
  items step can say how to report without declaring stages.
- A step's `profile` is inherited by its nested steps, loop body, and per-item
  stages, like agent, model, and reasoning already were; a nested step may
  still override it.
- `loop_assignment` on a loop wrapper: `per_iteration`, now the default, keeps
  consecutive body steps of one round in one worker assignment while they
  resolve to the same worker settings; `per_step` restores one assignment per
  body step. Every body step's instruction now names the loop round it is in.
- A loop that reached its iteration limit is no longer a dead end: `next`
  repeats the escalation instead of erroring, and `next --force --force-reason`
  leaves the loop and continues with the steps after it.
- `next --force` validates the task state before its confirmation prompt and
  states what the force will do.
- `artifacts` adds an absolute `path` beside each project-relative reference,
  for workers running in a linked worktree.

## 2026-09-23

The initial feature set, written before the repository was public.

- `workflows.yaml` compiled into an explicit, saved plan with steps, nested
  steps, hooks, handlers, loops, assessments, items, children, and handoffs.
- Manager and worker roles with `single` and `auto` runtimes, per-item worker
  assignments, and resumable, recoverable execution state under `.ww/`.
- `discover` for agents, a shippable `ww` skill, and an `enabled` switch.
- Optional `projects` for one ww instance over several repositories, with task
  working directories and per-project base branches.
- Children that bind their own external task IDs, for example one Jira story
  per child.
- Bundled `ww/git` extension for branches, worktrees, and recorded commits, and
  a public extension API.
