# Rules and checks: implementation plan

Status: agreed design, 2026-09-29. Nothing built. This plan is written for an
agent session that implements it through ww, one slice per task. Read it whole
before starting a slice; each slice depends on the vocabulary of the previous
ones. Where the plan says "confirm", the operator has not yet decided.

Every slice follows `documentation/python-agent-rules.md` and CONTRIBUTING.md:
parse → plan → execute stay separate, new persisted formats bump their schema
version, `documentation/specification.md` and `documentation/features.md` are
updated in the same change, `CHANGELOG.md` gets an entry, and new tests are
written in the develop step, never left for later.

## 1. What is being built, in one page

A **rule** is knowledge given to a step: a sentence the agent must follow while
working, optionally scoped to files by a glob. A **check** is evidence ww
collects after the step: a command ww runs, whose result decides whether the
step may complete. Each rule may carry its own check; a step may carry checks
of its own; and a rule without a check can be turned into one by a **verifier**
agent once, with the operator's approval, after which ww runs it forever.

Principles that every design choice below follows:

- The agent that did the work never grades its own work.
- Any claim that can be turned into a command is run by ww, never taken on
  trust from the agent.
- A failed check is by definition the agent's own deviation, so it always goes
  back to the agent (`fix`), never to the operator, until a limit is reached.
- A hook failure is by default the operator's decision, as today.
- The user-facing surface stays tiny: `rules` (root mapping and step list),
  `paths` and `check` in a rule file, `max_fixes`, `on_failure` on hooks,
  `operator: empty` in `assert`. Everything else reuses existing syntax.
- ww never rewrites YAML or rule files. Derived knowledge lives in a ww-owned
  store keyed by the hash of the rule text.
- Git is used directly by core for the change set; there is no other VCS and
  no snapshot fallback. Without git a glob simply selects all matching project
  files.

The full agreed syntax:

```markdown
<!-- rules/php/services-location.md : the ID is php-architecture/services-location -->
---
paths: ["src/**/*.php"]                  # optional
check:                                   # optional; the existing cli handler shape
  shell: find $WW_STEP_CHANGED_FILES -name '*Service.php' -not -path 'src/Service/*'
  assert: { operator: empty }
max_fixes: 5                             # optional per-rule override
model: claude-opus-5-5                   # optional; agent, model, reasoning
---
Put every `*Service.php` under `src/Service/<Domain>/`, one class per file.

Controllers must not instantiate services; inject them. (Rationale and
examples go here; only the first sentence is shown on the step page.)
```

```yaml
# ww-agentic-workflows.yaml
rules:                                   # root: named groups, active where their filters allow
  php-architecture: [rules/php/]         # a directory, files, or other group names
  docs-style:
    rules: [rules/docs/]
    workflows: [task, bugfix]            # intersected, like global hook filters
    steps: [develop, refactor]
    reasoning: high                      # optional agent/model/reasoning for its rules
  engineering: [php-architecture, docs-style]

workflows:
  task:
    steps:
      - develop:
          rules:                         # step: a list of step-local entries
            - Keep the public CLI unchanged.                    # judged rule (text only)
            - text: Include "foo" in every file you change.
              shell: grep -L foo $WW_STEP_CHANGED_FILES
              assert: { operator: empty }
            - argv: [vendor/bin/phpstan, analyse]               # command only
            - rules/one-off/no-migrations.md                    # a rule file
            - php-architecture                                  # a group declared with steps: []
          hooks:
            before_complete:
              - argv: [pytest]
                on_failure: fix          # hooks only: fix | operator (default)
```

```json
// ww-agentic-workflows.json
{ "max_fixes": 3 }
```

## 2. Facts about the current code the plan relies on

Gathered on 2026-09-29; verify line numbers before editing.

- Root YAML keys are allow-listed in `src/ww/config/__init__.py` `_raw_from_text`
  (`modes, profiles, documents, handlers, hooks, workflows, tasks`); parsing
  order is in `parse_yaml_text`; semantic validation is
  `src/ww/workflow_validation.py` `validate_configuration`.
- Unknown keys are rejected through `src/ww/config/values.py` `_only`. Step
  keys: `STEP_ONLY_KEYS` in `src/ww/config/steps.py`; `_parse_step` is the step
  parser. Hook parsing: `src/ww/config/actions.py` `_parse_hooks`,
  `_parse_hook`, `HOOK_PHASES`.
- The cli handler shape (`argv` | `shell`+`args`+`env`, `assert`,
  `idempotent`) is parsed by `src/ww/actions/command.py` `CommandAction.parse`
  (`_parse_command_action`, `_parse_assertion`, which accepts only
  `operator: eq`); models in `src/ww/actions/contracts.py`; run-time rendering
  in `render_command`; evaluation in `CommandAction.execute`; the process is
  launched by `src/ww/action_execution.py` `_CommandService.execute` with
  `subprocess.Popen(..., env={**os.environ, **request.environment, WW_*})`.
- JSON settings are parsed by `src/ww/project_config.py` `_parse_settings`
  (allow-list `enabled, runtime, extensions, builtins, loop_max_times,
  projects, update_check, workflows, executable, task_format`) into
  `ProjectConfig`. `loop_max_times` is already a JSON setting with default 3
  and a per-loop YAML override; per-project files contribute only
  `extensions` and `task_format` (`PROJECT_FILE_KEYS`).
- YAML composition across machine/repo/local levels and `imports` is
  `src/ww/config/composition.py` `_Composer.apply`; named catalogs merge by
  name, `hooks` append.
- Extensions contribute only Python objects: `src/ww/extensions/api.py`
  `Extension(handlers, modes, commands, variables, ...)`; no YAML, no hooks.
  Discovery: `src/ww/extensions/registry.py` (`_bundled_providers`,
  `_project_providers` from `<root>/ext/*/*/extension.py`, entry points).
  Extension durable state: `ExtensionStore` at `<root>/.ww/ext/<vendor>/<name>/`.
- Plan compilation: `src/ww/plan/compiler.py` `WorkflowPlanCompiler.compile`,
  `_compile_steps` (before_in_progress hooks → construct → before_complete →
  after_complete), `_append_hooks` uses `HookDefinition.applies_to`,
  `_append_handler` builds `PlanItem` with `ExecutionHints.overlay`.
  `PLAN_SCHEMA_VERSION = 11` in `src/ww/execution_models/runs.py`.
- Step page: `src/ww/instructions/builder.py` `InstructionBuilder._active`
  fills the `Instruction` dataclass (`src/ww/instructions/models.py`,
  `to_dict`); `src/ww/output_adapters/markdown.py` `render_instruction` calls
  one section function per section (`_task_requirements`, `_work`, ...,
  `_continuation`), `_append_section(lines, title)` writes `### Title`.
- Completion: `src/ww/service.py` `WorkflowService.complete` → `_complete`
  (validate inputs → artifact required → `write_completion_artifacts` →
  `transitions.complete_agent_item` → commit → `drain` automatic items).
  Handler failure: `ActionExecutor._fail_item` sets `state.status="failed"`;
  `src/ww/instructions/policy.py` `operator_reason` / `_control` produce
  `awaiting_operator`; literals in `src/ww/contracts.py` `OperatorReason`.
  Recovery: `service.next` with `--retry` / `--force --force-reason`,
  `src/ww/recovery.py`, `transitions.retry_failed_item` / `skip_failed_item`.
- State: `src/ww/execution_models/records.py` `PlanItemExecution` (per item),
  `StepProgress`, `ExecutionState`; `EXECUTION_SCHEMA_VERSION = 6`; new fields
  are added with defaults and read with `data.get`. An agent item enters
  `in_progress` in `transitions.begin_agent_item` (called from `service._next`
  and `drain`). Per-task files under `.ww/tasks/<id>/` (`state.json`,
  `metadata.json`, `items.json`, `runs/<run>/steps/*.md`,
  `runs/<run>/command-output/`).
- Core already runs git directly (`cli/main.py` `_resolve_project_root`,
  `cli/initialization.py` `_git_base_branch`); the task's working directory
  is `ExecutionState.working_directory`, resolved by `src/ww/workspace.py`
  and `ActionExecutor.item_scope`.
- Artifacts: `src/ww/completion_artifacts.py` `write_completion_artifacts`,
  `src/ww/artifacts.py` `render_step_artifact` (fixed layout `# id — step`,
  `## Workflow context`, `## Result`); `WorkflowService.artifacts` lists them.
- Lint: `src/ww/cli/main.py` `_lint` (plain text, no `--json`), errors are
  whatever `ConfigurationError`s parsing and validation raise; composition
  `notices` are printed as `Notice:` lines.
- Skills: `src/ww/assets/<name>_skill.md`, registered in `src/ww/defaults.py`
  `SKILLS` tuple `(WW_SKILL_NAME, "noww")`, installed by `init` through
  `cli/initialization.py` `_skill_installs` and `Storage._initialize_project`
  (never overwrites; a newly bundled skill is offered once).
- CLI: `src/ww/cli/parser.py` `build_parser` (`_shared("json")` adds
  `--json`), `src/ww/cli/main.py` `_HANDLERS`, `_READ_ONLY_COMMANDS`,
  `_MACHINE_READABLE_COMMANDS`, `src/ww/output.py` `render_*`.
- Tests: `tests/unit/`, `tests/integration/`; end-to-end pattern = write YAML
  (+JSON) to `tmp_path`, `WorkflowService(Storage(tmp_path))`, `start` /
  `next` / `complete`, assert on `JsonOutputAdapter` or
  `MarkdownOutputAdapter` output; helpers in `tests/workflow_helpers.py`; git
  repositories via the `repository` fixture pattern in
  `tests/integration/test_git_extension.py`; `tests/unit/test_examples_doc.py`
  compiles every YAML/JSON block in `documentation/examples.md`.

## 3. Decisions made while mapping the code (confirm with the operator)

1. **Extensions ship rule groups through the Python API**, not YAML, because
   extensions cannot contribute YAML today. `Extension` gains an optional
   `rules: tuple[RuleGroupContribution, ...]`, each naming a group, a directory
   or files relative to the extension file, and optional `workflows`/`steps`
   filters and agent hints. The registry exposes them and the YAML parser
   merges them under root `rules` before validation. A group name declared in
   both YAML and an extension is a `ConfigurationError`. A project therefore
   gets rules by listing the extension in its JSON `extensions`, which keeps
   the per-project JSON contract (`extensions` + `task_format`) unchanged.
2. **`loop_max_times` stays where it is.** It is already a JSON setting with a
   YAML per-loop override; `max_fixes` follows the same pattern.
3. **`ww rules add` does not commit.** Core has no commit capability (only the
   git extension does). Rule files are configuration; committing them is the
   operator's or the surrounding task's job, as for any YAML edit.
4. **Checks run inside `complete`, before the completion is recorded.** A
   failed check leaves the step in progress and the artifact unwritten, which
   is what "completion rejected" means. `before_complete` hooks with
   `on_failure: fix` are compiled into the same check list rather than into
   hook plan items, so there is exactly one rejection mechanism.
5. **Store lookups happen when the step begins, not at compile time.** The
   plan freezes each rule's text and hash; the resolution of "which command
   runs for this hash" is read from the store in `begin_agent_item` and saved
   on the item's execution record. Execution never recompiles the plan, and a
   command approved mid-task applies to the next step that begins.
6. **`$WW_STEP_CHANGED_FILES` is newline-separated**, project-relative paths.
   Word-splitting in `sh` handles it for `grep -L foo $WW_STEP_CHANGED_FILES`
   and `xargs`; paths containing whitespace are documented as unsupported in
   a bare expansion (use `printf '%s\n' "$WW_STEP_CHANGED_FILES" | xargs -d '\n'`).

## 4. Slices

Each slice is one ww `task`. Do not start a later slice before the previous
one is merged. Within a slice, the order of sections is the suggested order
of work.

### Slice 1 — Rules, delivery, change set, checks, fix loop

Goal: rules are declared, shown on the step page, and every rule or hook that
has a command is enforced at completion with the fix loop. Judged rules are
delivered and reported, not yet verified (slice 2).

#### 1.1 Rule files and the root `rules` mapping (parse)

New module `src/ww/config/rules.py`:

- `RuleDefinition` (frozen dataclass): `id: str` (`<group>/<file-stem>` or
  `<step-name>/<ordinal>` for step-local entries), `text: str` (body, stripped),
  `summary: str` (first sentence of `text`; split on the first `.`, `!`, `?`
  followed by whitespace or end, or the first line if none), `paths:
  tuple[str, ...]`, `check: CommandCheck | None`, `max_fixes: int | None`,
  `hints: ExecutionHints` (agent/model/reasoning), `source: Path`, `text_hash:
  str` (sha256 of `text` after whitespace normalisation: strip, collapse runs
  of whitespace to one space).
- `RuleGroup`: `name`, `rules: tuple[RuleDefinition, ...]`, `workflows`,
  `steps` (same filter semantics and types as `HookDefinition`), `hints`.
- `parse_rule_file(path, group) -> RuleDefinition`: YAML frontmatter between
  the first two `---` lines (reject a file without a body; frontmatter is
  optional). Allowed frontmatter keys: `paths` (non-empty string list),
  `check`, `max_fixes` (positive int), `agent`, `model`, `reasoning`.
  `check` is parsed with the existing `CommandAction.parse` shape restricted
  to `argv`/`shell`/`args`/`env`/`assert` (no `idempotent`, no `command:`).
  Anything else: `ConfigurationError` naming the file and key.
- `parse_rules_root(raw, source_file, extension_groups) -> dict[str,
  RuleGroup]`: a group is a list (items) or a mapping with `rules` (items) and
  optional `workflows`, `steps`, `agent`, `model`, `reasoning`. An item is a
  string resolved in this order: an existing group name; else a path relative
  to the declaring file (directory: every `*.md` directly inside it, sorted;
  file: that rule). A path that does not exist is an error naming the group
  and item. Group references may nest; a cycle is an error. An absolute path
  is accepted and produces a composition notice `Notice: rule path ... is
  absolute` (printed by lint). Rule IDs inside a group come from the file
  stem; two files with the same stem in one group is an error; a rule reached
  through two groups keeps the ID of each group it is reached from (the file
  is parsed once and the definition copied with the other ID).
- Root allow-list in `_raw_from_text` gains `rules`. Composition
  (`_Composer.apply`): `rules` merges by group name, later level replaces
  the whole group; imports contribute the same way. Extension groups are
  merged before YAML groups; a name clash is an error (decision 3.1).

Step-local `rules` (in `_parse_step`, `STEP_ONLY_KEYS` gains `rules`): a list
whose entries are a string or a mapping.

- String: an existing group name → that group, filtered to this step
  regardless of the group's own filters (the step asked for it explicitly);
  else a path that exists relative to the declaring file → a rule file or
  directory; else the literal rule text.
- Mapping: keys `text`, `argv`/`shell`/`args`/`env`, `assert`, `max_fixes`,
  `agent`, `model`, `reasoning`. At least one of `text` or a command is
  required. `text` without a command is a judged rule; a command without
  `text` is a pure mechanical check whose `summary` is the command itself.
- Step-local IDs are `<step-name>/<1-based ordinal>`; groups pulled in keep
  their own IDs.
- Stored on `StepDefinition.rules: tuple[RuleDefinition | RuleGroupRef, ...]`
  (`src/ww/workflow_config.py`). Steps that are bare handler references may
  not carry `rules` (same treatment as other `_STEP_CONTENT_KEYS`).

`assert` gains `operator: empty` in `_parse_assertion` (`expected` must be
absent with `empty`, required with `eq`) and in `AssertionDefinition`; the
codec (`_assertion_from_dict`) and `CommandAction.execute` handle it
(`stdout.strip() == ""`). This applies to every cli handler, not only checks.

Hooks: `_parse_hook` accepts `on_failure: fix | operator` (default
`operator`) on every hook entry and on each member of a `handlers` group.
`HookDefinition.on_failure`.

#### 1.2 Validation (`workflow_validation.py`)

Add `_validate_rules`:

- Group `workflows` and `steps` filters name existing workflows and steps,
  same rules and error wording as hook filters (reuse the hook filter
  validation helpers; extract them if they are inline).
- A step-local entry naming a group that does not exist is an error only if
  the string looks like a reference and not like a sentence: define "looks
  like a reference" as matching `^[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)?$` with no
  whitespace; such a string that resolves to neither a group nor a path is an
  error `step X rule 'Y' names no group or file`. Anything with whitespace or
  ending in punctuation is text.
- `on_failure: fix` is allowed only on `before_complete` hooks (global,
  workflow, or step scope); elsewhere `ConfigurationError("on_failure: fix is
  only valid on before_complete hooks")`. It is also an error on a
  `before_complete` hook that carries a `workflow` transition.
- A check's `argv[0]` / shell text is not validated against any allowlist
  (hand-written commands are the operator's own; derived ones are approved
  in two stages, slice 2).

`ww lint` prints, after the existing lines, `Rules: N groups, M rules` and any
notices (absolute paths).

#### 1.3 Plan compilation

- `PlanItem` gains `rules: tuple[PlannedRule, ...]` and `checks:
  tuple[PlannedCheck, ...]` (`src/ww/plan/models.py`, `to_dict`, codec).
  `PlannedRule = {id, summary, text, text_hash, paths, has_command, max_fixes,
  hints}`; `PlannedCheck = {id, source: "rule" | "hook", planned_command:
  Commands | None (already placeholder-substituted like other cli handlers),
  assertion, paths, max_fixes}`.
- In `_compile_steps`, for every agent step item (not init, not hooks, not
  loop boundaries, not the built-in summary prompt): collect groups whose
  filters admit `(workflow.name, step.name, step_path)` using the same
  matching as `HookDefinition.applies_to`, plus the step's own entries,
  in declaration order: root groups in YAML order, then step-local entries.
  Deduplicate by rule ID, first occurrence wins.
- `before_complete` hooks with `on_failure: fix` for that step become
  `PlannedCheck(source="hook")` entries appended after the rule checks, in
  hook order; they are **not** emitted as hook plan items. Hooks with
  `operator` are compiled exactly as today.
- `max_fixes` resolution: rule value, else `ProjectConfig.max_fixes`.
- Judged rules (no command) appear only in `rules`, not in `checks`.
- Bump `PLAN_SCHEMA_VERSION` to 12. Add a fixture to
  `tests/fixtures/` or extend `action_plan_compatibility.json` if it covers
  plan items.

#### 1.4 JSON setting `max_fixes`

`_parse_settings`: allow `max_fixes` (positive int, default
`DEFAULT_MAX_FIXES = 3`), field on `ProjectConfig`, entry in
`DEFAULT_PROJECT_CONFIG_JSON` with the module docstring example. The compiler
reads it like `loop_max_times`.

#### 1.5 Change set (`src/ww/changes.py`, core, git only)

- `take_mark(workdir: Path) -> str | None`: if `git rev-parse --is-inside-work-tree`
  succeeds in `workdir`, run with `GIT_INDEX_FILE=<tmp copy of the current
  index, or empty tmp file when there is no index>`: `git add -A` then `git
  write-tree`; return the tree id. Return `None` when git is missing or the
  directory is not a repository. Never touch the real index or the stash.
  Delete the temporary index afterwards.
- `changed_files(workdir, mark_a, mark_b) -> tuple[str, ...]`: `git
  diff-tree -r --name-only --diff-filter=ACMR <a> <b>`, project-relative,
  sorted. Deleted files are excluded (nothing to check in them).
- `select_files(files, paths_globs, workdir, *, all_when_unmarked: bool)`:
  glob matching with `fnmatch` semantics extended so `**` matches across
  directories (use `pathlib.PurePath.match` or `wcmatch`-free hand-written
  matcher; keep it small and tested). When `mark_a is None` (no git),
  the candidate list is every file under `workdir` excluding `.git`, `.ww`,
  and the configured worktree directory, and the result is flagged
  `all_files=True`.
- Persisted: `PlanItemExecution.change_mark: str | None`, taken in
  `transitions.begin_agent_item` (which needs the workdir: pass it in from the
  callers, `service._next` and `drain`, using `item_scope`). Bump
  `EXECUTION_SCHEMA_VERSION` to 7. Older state without the field loads with
  `None`, which behaves as "no git".
- The rule engine (1.6) takes the second mark at check time; it is not
  persisted except in the check results.

#### 1.6 Check execution and the fix loop (`src/ww/rule_checks.py`)

`RuleChecker.run(state, snapshot, item, executor) -> CheckReport`:

1. `mark_b = take_mark(workdir)`; `files = changed_files(...)` or the
   all-files list.
2. For each `PlannedCheck` in order: `selected = select_files(files,
   check.paths)`. Empty `selected` with a non-empty `paths` → result
   `not_applicable`, command not run. Otherwise run the planned command
   through the existing `_CommandService.execute` path with an extra
   environment entry `WW_STEP_CHANGED_FILES="\n".join(selected)` (for a
   check without `paths`, the whole change set). Reuse `ActionExecutor`'s
   command-output writing so `ww artifacts` lists check outputs under the
   step with a `check` marker. A check failing means: non-zero exit, or the
   assertion failing. Record `{id, status: passed|failed|not_applicable,
   output (last 40 lines), command}`.
3. Judged rules are reported as `self_declared` (slice 2 replaces this with
   verdicts).
4. `CheckReport.failed` is the list of failed results.

In `service._complete`, after input validation and the artifact-required
check and **before** `write_completion_artifacts`:

- If the item has checks: run the checker. If nothing failed, continue as
  today and attach the report to the record (`PlanItemExecution.check_results`)
  and to the artifact (1.8).
- If something failed: `record.fix_attempts += 1`; save the report on the
  record (`last_check_report`); commit; return an `Instruction` with
  `control="continue_worker"`, `next_role="worker"`, new field
  `fix_required: FixRequired {attempt, max_fixes, failures}`. The step stays
  `in_progress`; the supplied artifact is kept as `draft_artifact` on the
  record so the page can say "your previous artifact is kept; revise it".
  The `complete` CLI exits non-zero (like a handler failure), so the agent
  reads the whole response.
- `max_fixes` per step = the maximum over its checks' `max_fixes`, evaluated
  per check: a check that has failed `max_fixes` times sets
  `state.status="failed"`, `state.last_error="check limit reached: <ids>"`
  and a new `OperatorReason` literal `fix_limit`. `policy.operator_reason`
  returns it when `state.last_error` starts with the marker (or, cleaner, a
  dedicated `state.failure_kind` field; choose the cleaner one and use it for
  the other reasons only if it is a small, safe refactor).
- Recovery from `fix_limit`: `next --retry` clears `fix_attempts` for the
  step and returns it to `in_progress` for the same agent; `next --force
  --force-reason` sets `record.checks_waived = reason` so the next `complete`
  skips the checks and records the waiver in the artifact.
- `fix_attempts` resets when a loop round starts the step again (it is a new
  `PlanItemExecution` per iteration already; verify).

#### 1.7 Delivery on the step page

- `Instruction.rules: tuple[RuleLine, ...]` (`{id, summary, paths,
  has_command, interpretation: str | None}`) filled in
  `InstructionBuilder._active` for the active agent item from
  `item.rules`; `Instruction.fix_required` for rejections; `to_dict` for both.
- Markdown `_rules(lines, instruction)` after `_work`:

  ```markdown
  ### Rules

  Follow these while working. ww checks them when you complete; run
  `wwdev check TASK-19` at any time to see the result without completing
  (slice 3; until then omit this sentence). Read a full rule with
  `wwdev rule TASK-19 <id>` (slice 3; omit until then).

  - `python/contract-first` — State the observable behaviour being changed and the invariants that must hold.
  - `python/services-location` — src/**/*.php — Put every `*Service.php` under `src/Service/<Domain>/`.
  - `develop/1` — Keep the public CLI unchanged.

  Checked automatically when you complete: `python/no-print`, `docs/changelog-entry`, `develop/2`.

  State in your artifact, under a **Rules** heading, which rules you applied
  and any deviation with its reason.
  ```

  Rules with a command are collapsed into the "Checked automatically" line
  (IDs only); rules without one get a bullet. The glob is shown when present.
  The delegate page (auto runtime) includes the section for the worker.
- Markdown `_fix_required` rendered instead of the normal sections when
  `fix_required` is set:

  ```markdown
  ## Fix required: 2 of 5 checks failed (attempt 1 of 3)

  ### `python/services-location`
  Put every `*Service.php` under `src/Service/<Domain>/`.

  Command: find $WW_STEP_CHANGED_FILES -name '*Service.php' -not -path 'src/Service/*'
  Output:
      src/Controller/ReportService.php

  ### `develop/pytest` (hook)
  Command: pytest
  Output: <last 40 lines>

  Fix the causes, then complete again with a revised artifact. Your previous
  artifact is kept as a draft.
  ```

  followed by the same completion command as the step page.

#### 1.8 Artifact

`render_step_artifact` gains an optional `rules` block rendered as `## Rules`
after `## Result`: one line per rule with its status (`passed`, `failed`
(only possible with a waiver), `not applicable`, `self-declared`), the
waiver reason when present, and the number of fix attempts. Hook checks are
listed with `(hook)`.

#### 1.9 Runtime `auto`

The rules section is part of the delegate page; the fix page is returned to
whoever ran `complete`, which is the worker under `auto`; nothing else
changes. Verify `handoff_manager` is not emitted on a rejected completion.

#### 1.10 Documentation, changelog, tests

- `specification.md`: new `## Rules` section (rule file format, root
  mapping, step list, ID scheme, `max_fixes`), `assert` gains `empty`, hooks
  gain `on_failure`; `features.md`: a "Rules and checks" section (what,
  delivery, the fix loop, the change set and its git-only nature,
  `WW_STEP_CHANGED_FILES`, artifact section, `fix_limit` in the operator
  table); `examples.md`: one complete example (it is compiled by
  `test_examples_doc.py`, so it doubles as a test).
- `CHANGELOG.md` entry.
- Tests (write them in develop):
  - unit: rule file parsing (frontmatter, missing body, unknown key, check
    shapes, summary extraction, hash normalisation); root mapping resolution
    (list/mapping forms, nesting, cycle, missing path, absolute notice,
    directory ordering, duplicate stems); step entries (group/path/text
    resolution, reference-looking string error, mapping forms); `assert:
    empty`; `on_failure` validation; composition merge by name; extension
    contribution and clash; compiler: groups filtered per step, dedupe,
    hook-to-check conversion, `max_fixes` resolution, plan codec round-trip
    with schema 12; `changes.py` on a temp git repo (commit during the step,
    staged, unstaged, untracked, pre-existing dirt cancels out, deleted files
    excluded) and without git (all files, excluded dirs); glob matcher.
  - integration: end-to-end with a YAML that has one judged rule, one rule
    with a command, one hook with `fix`, one `operator` hook: page shows the
    section; complete with a violation → `continue_worker` + fix page +
    non-zero exit + step still in progress + no artifact written; fix and
    complete → passes, artifact has `## Rules`; third failure → `fix_limit`
    + `next --retry` / `--force` behaviours; `not_applicable` when the glob
    matches nothing; `operator` hook still yields `handler_failed`; state
    round-trip with the new fields (`test_state_unknown_fields.py` pattern);
    `ww lint` line.

Acceptance for slice 1: every bullet above has a test; `documentation/examples.md`
compiles; `python-agent-rules.md` is **not** yet split (that is slice 4's
skill job, done as a separate task with the skill).

### Slice 2 — Verifier, automation store, approval gate

Goal: rules without a command get interpreted, scriptized, and judged by an
agent that is not the developer; derived commands never run without the
operator's approval; nothing is reasoned about twice.

#### 2.1 Store (`src/ww/rule_store.py`)

- File `<root>/.ww/rule-automation.json`, committed (document that it is not
  in `.gitignore`; `init --update-gitignore` must not add it). Two maps:
  `rules`, keyed by `text_hash`, and `checks`, keyed by a short check name
  the verifier chooses (kebab-case, unique). One check may cover many rules;
  that is the normal case for ecosystem tools such as deptrac, PHPStan,
  import-linter or eslint, where several rules become lines in one tool's
  configuration and one command runs them all.

  ```json
  {
    "rules": {
      "9f2a…": {
        "text": "Controllers must not instantiate services; inject them.",
        "interpretation": "No `new *Service(` in src/Controller; constructor injection only.",
        "status": "converted",
        "check": "deptrac",
        "approach": "deptrac layer rule: Controller may not depend on Service constructors",
        "proposed_in": "TASK-19/01-task/develop"
      },
      "b31c…": {"text": "...", "status": "approach-proposed",
                "approach": "grep over the changed files for ...",
                "interpretation": "..."},
      "41c0…": {"text": "...", "status": "not-convertible", "reason": "..."},
      "c7d1…": {"text": "...", "status": "ambiguous",
                "candidates": ["changed files", "all files"]},
      "e001…": {"text": "...", "status": "rejected", "reason": "operator: ..."}
    },
    "checks": {
      "deptrac": {
        "argv": ["vendor/bin/deptrac", "analyse", "--no-progress"],
        "assert": null,
        "config": ["deptrac.yaml"],
        "covers": ["9f2a…"],
        "status": "converted",
        "proven": true,
        "proposed_at": "2026-09-29T10:00:00Z",
        "approved_at": "2026-09-29T11:00:00Z"
      },
      "foo-header": {"shell": "grep -L foo $WW_STEP_CHANGED_FILES",
                     "assert": {"operator": "empty"}, "covers": ["b31c…"],
                     "status": "proposed", "proven": true}
    }
  }
  ```

  Rule `status ∈ {approach-proposed, approach-approved, proposed, converted,
  rejected, not-convertible, ambiguous}`; check `status ∈ {proposed,
  converted, rejected}`. Only a `converted` check is ever run, and a rule
  counts as mechanical only when its `check` names a `converted` check.
  `config` lists the project files that carry the check's logic, so a later
  rule can be added to the same tool. `RuleStore.load/save` with atomic
  write and the same revision discipline as other ww files (read-modify-write
  under the task lock; the file is shared across tasks, so use a dedicated
  file lock like the project metadata lock).
- Normalisation and hashing live in `config/rules.py` (slice 1) and are the
  single source of truth for the key.

#### 2.2 Resolution at step start

In `begin_agent_item` (with the store passed in), for every `PlannedRule`
without a command: look up its hash. `converted` with a `converted` check →
materialise one `PlannedCheck` per distinct check name into
`record.resolved_checks`, carrying the list of rule IDs it covers (a check
covering five rules runs once); `rejected`/`not-convertible` → judged;
`ambiguous`/`approach-proposed`/`approach-approved`/`proposed` → judged for
now and flagged `pending_operator` on the page ("this rule has a pending
proposal; the operator has not yet decided"); miss → `unresolved`. The page
section (1.7) shows a `converted` rule under "Checked automatically" and a
judged one with its `interpretation` on a second line when the store has one.
A failing shared check is reported once, under its check name, listing the
rule IDs it covers; the fix page shows those rules' texts beneath it.

#### 2.3 The verification assignment

After a `complete` whose mechanical checks passed, if the item has any
`unresolved` or judged rules, ww does not record the completion yet. It
creates a **verification item**: a ww-generated agent plan item (same
mechanism as the built-in `update-workflow-summary` prompt item), id
`<workflow>:<step_path>:verify:<n>`, owner agent, hints = the rule's/group's
`agent`/`model`/`reasoning` else the step's; one item per distinct resolved
hint set, each covering the rules that resolve to it. The developer's
artifact is held as `draft_artifact` meanwhile.

Scriptizing happens in **two stages**, each ending in an operator stop, so
the operator steers before anything is built and confirms what was built:

- **Stage A, approach**: for every `unresolved` rule of the step at once, the
  verifier reports an interpretation and a one-line approach: which tool or
  command would check it, and which rules share one check. It writes no
  command and installs nothing. Result per rule: `approach` (text, plus the
  proposed check name and whether it is an existing check to extend or a new
  one), `not-convertible` with a reason, or `ambiguous` with candidates.
- **Stage B, prepare**: run only for rules whose approach the operator
  approved (2.4). The verifier builds the check: installs the tool as a dev
  dependency in the project's manifest if needed, writes or extends its
  configuration in the repo, proves the check, and reports the command.
  Result per check: the command, assertion, `config` files, `covers`,
  `proven`.

The verification page (new builder branch, new markdown section) contains
the stage, and per rule: ID, text, its state (`unresolved` for stage A,
`approach-approved` with the approved approach for stage B, or `judged` for
a verdict), the change set (the file list, and the diff command to run: `git
diff <mark_a> <mark_b>` when git is present), the developer's draft artifact
path, and the store's existing `checks` with their `covers` and `config`
files. Instructions, verbatim intent:

- Interpret each rule in one sentence; if more than one reasonable reading
  exists, report `ambiguous` with the candidates and stop on that rule.
- Propose the fewest checks for the rules below; a check may cover several
  rules. Prefer the ecosystem's own tools (for PHP deptrac, PHPStan or
  Psalm rules; for Python import-linter, ruff or a pytest architecture
  test; for JavaScript eslint rules), because they express many rules in one
  configuration and run once. Before proposing a new check, look at the
  existing checks on this page: if an existing tool can express the rule,
  propose extending its configuration, naming the check. Use plain shell
  (`grep`, `find`, `git`, `sed`, `awk`) only for what no tool covers.
- Stage B only: install a tool into the project's manifest as a development
  dependency, never globally; keep its configuration in the repo and list
  the files under `config`. The command must run offline. Prove every
  check: construct a deliberately violating input in a temporary copy and
  show the command fails there and passes on the real change set. Report
  `not-convertible` with a reason when preparation shows the approach does
  not work.
- For judged rules and for rules you could not convert: give a verdict
  `pass` or `fail` with evidence `file:line — what` for each failure.
- Under `single` the same session performs this; say so on the page as a
  note, not as a different instruction.

Completion command for the item: `wwdev complete <task> --role worker
--artifact "<markdown>" --rule-result '<json>'` repeated per rule and
`--check-result '<json>'` repeated per check (stage B). A rule result is
`{id, interpretation?, status: approach|not-convertible|ambiguous|judged,
check?: <name>, approach?, reason?, candidates?, verdict?: pass|fail,
failures?: [{file, line?, what}]}`; a check result is `{name, argv?|shell?,
args?, env?, assert?, config: [paths], covers: [rule ids], proven: bool}`.
`complete` validates one rule result per rule the item covers, one check
result per check named by an `approach-approved` rule, and refuses
otherwise. (These are new repeatable options on the existing `complete`
parser; on ordinary steps they are an error.)

#### 2.4 What ww does with the results

In order, inside the same `complete`:

1. Write store entries. Stage A: rule `approach-proposed` with `approach`,
   `interpretation` and the proposed check name; `not-convertible`;
   `ambiguous`. Stage B: check entries as `proposed` (never `converted`
   directly) and their rules as `proposed`. A rule hash that already has an
   entry beyond `approach-approved` is not overwritten (the verifier should
   not have been asked; log a notice). An existing `converted` check that
   the verifier extended gets a new `proposed` revision beside the current
   one (`checks.<name>.pending`), so the running check stays the approved
   one until the operator approves the extension.
2. Any `fail` verdict → the original step's fix loop (1.6): `fix_attempts`
   +1, fix page with the judged failures listed as `file:line — what`, the
   verification item is reset to pending, the developer completes again.
   (Judged failures count toward the same `max_fixes`.)
3. Otherwise, if any rule ended `approach-proposed`, `proposed` or
   `ambiguous`: `awaiting_operator` with `operator_reason` `check_proposed`
   (all kinds on one page; a new literal; the stage and the ambiguity are
   per-rule kinds rather than separate reasons, to keep one stop). The page
   lists, per rule: text, interpretation, and for stage A the approach and
   the check it would join or create; for stage B the check's command,
   assertion, config files, the rules it covers, and the proof result; for
   an ambiguous rule its candidates. Operator choices, rendered as recovery
   commands:
   - `next <task> --approve <hash|check-name>` (repeatable). On a rule in
     `approach-proposed`: marks it `approach-approved`, and when every
     pending rule of the stop is decided, ww re-dispatches the verification
     item for stage B on the approved rules, still holding the completion.
     On a check in `proposed`: marks the check and its covered rules
     `converted` with `approved_at`; a pending revision replaces the current
     one. There is no executable allowlist: the operator reads the approach
     and then the command, and that is the safety; the CLI prints the
     command in full before recording the approval.
   - `next <task> --approach <hash> "<text>"`: replaces the verifier's
     approach with the operator's own sentence and marks the rule
     `approach-approved`, so stage B builds what the operator asked for.
   - `next <task> --pick <hash>=<candidate index>` for an ambiguous rule:
     stores the interpretation, leaves the rule judged, and re-runs stage A
     for that rule with the interpretation fixed.
   - `next <task> --force --force-reason "<why>"`: every pending rule or
     check of this stop becomes `rejected` with the reason; the completion
     proceeds with those rules judged as they were.
   After a check is approved, it runs immediately on the held completion
   (it may fail → fix loop).
4. When nothing is pending and nothing failed: record the developer's
   completion with the held draft artifact, attach the verdicts to the
   `## Rules` section (`verified: pass` with the verifier item id), and
   continue.

Cost of the two stages: two operator stops per rule wording, once ever,
both batched per step. A project whose rules are stable stops seeing them.

#### 2.5 Lint and hygiene

`ww lint` reports: store entries whose hash matches no rule in the composed
configuration (`orphan`), entries in `proposed` or `ambiguous` (`pending`),
and offers nothing else (no automatic pruning; `ww rules prune` is slice 3).

#### 2.6 Documentation, changelog, tests

- `features.md`: "How a rule becomes a check" (the store with its rules and
  checks, statuses, the two stages, shared checks and ecosystem tools, the
  approval gate, what `single` means here); `specification.md`:
  `--rule-result`, `--check-result`, `--approve`, `--approach`, `--pick`;
  operator table gains `check_proposed`.
- Tests: store load/save/lock, hash stability against whitespace; resolution
  at step start for every status, one planned check per shared check name;
  verification item creation, one per hint set, under `single` and `auto`,
  stage A then stage B; `--rule-result` / `--check-result` validation
  (missing rule, unknown rule, bad JSON, bad status, check named by no
  rule); store writes, the never-overwrite rule, a pending revision of an
  existing check; judged `fail` → fix loop and counter; stage A →
  `check_proposed` page → `--approve` all → stage B dispatched → `proposed`
  → `check_proposed` page → `--approve <check>` → check runs on the held
  completion; failure after approval → fix loop; `--approach` replaces the
  approach; `--pick`; `--force` rejects all pending; a shared check failing
  is reported once with all covered rules; a task started in `single`
  produces the same store entries; lint orphans and pending.

### Slice 3 — Agent and operator commands

Goal: the agent can pre-check and dispute; the operator can inspect rules.

- `wwdev check <task> [--json]`: runs the checker (1.6) against the current
  change set for the active agent item without counting a fix attempt and
  without touching the record; output is the fix page format (or "all checks
  pass") plus, for judged rules, "judged at completion". Read-only command.
- `wwdev dispute <task> --rule <id> --reason "<why>"`: allowed only while the
  step is in progress and after at least one rejection naming that rule
  (otherwise: "nothing to dispute; run check first"). Records
  `{rule, reason, last output, attempt}` on the record, sets
  `awaiting_operator` with `operator_reason` `check_disputed`; the page shows
  rule text, command, output and the agent's argument. `next --retry` clears
  the dispute (the check stands; the counter is unchanged); `next --force
  --force-reason` sets `checks_waived` for that one rule ID (extend
  `checks_waived` to a mapping `rule id → reason`). Nothing mutates the
  store; `ww lint` lists rules that have ever been disputed (read from task
  states is too expensive: write a `disputes` list into the store entry, or
  a sibling file `.ww/rule-disputes.json`; choose the sibling file so the
  store stays "derived knowledge only").
- `wwdev rule <task> <id>`: prints one rule's full text, glob, check (or
  store status), and where it comes from. `wwdev rules [--json]`: groups with
  filters and their rule IDs and summaries; `wwdev rules prune`: deletes
  orphan store entries after listing them and asking (same confirmation
  helper as `next --force`).
- Page updates: the sentences omitted in 1.7 are added now.
- Docs, changelog, tests for each command (read-only sets, JSON output,
  error paths).

### Slice 4 — The `ww-rule` skill and its CLI writes

Goal: an operator can add or amend rules in conversation without learning
the format; the skill carries the judgment, the CLI does validated writes.

- CLI (all write to files, never commit; all run `load_configuration`
  afterwards and refuse the write if validation would fail, by validating a
  composed in-memory copy first):
  - `wwdev rules add <group> --text "<sentence and body>" [--paths g ...]
    [--check-shell "<sh>" | --check-argv a b c] [--assert empty|eq:<v>]
    [--id <stem>]`: creates `<group dir>/<stem>.md` where `<group dir>` is
    the group's first directory item (error if the group has none), stem
    derived from the first sentence (kebab-case, ≤ 5 words) unless given;
    refuses to overwrite.
  - `wwdev rules add --group <name> --dir <path> [--workflows ...] [--steps
    ...]`: adds a new root group to the repo-level YAML by editing only the
    `rules:` mapping (use a YAML round-trip that preserves the rest of the
    file; if the project has no acceptable round-trip dependency, write the
    group as a `rules:` mapping in a new import file `ww-rules.yaml` and
    add it to `imports`; confirm this choice with the operator).
  - `wwdev rules edit <id> --text ...` (replaces the body, keeps frontmatter;
    prints a warning that the hash changes and lists the store entry that will
    stop matching), `wwdev rules move <id> <group>`, `wwdev rules filter
    <group> --workflows ... --steps ...`, `wwdev rules promote <check-name>`
    (copies a `converted` check's command into the `check` frontmatter of
    every rule file it covers, or refuses when a covered rule is step-local,
    and deletes the check and marks its rules as carrying their own check).
- Skill `src/ww/assets/ww-rule_skill.md`, name `ww-rule`, added to the
  `SKILLS` tuple so `init` offers it once. Its instructions, in order:
  1. Run `wwdev rules --json` and `wwdev discover` to learn groups, filters,
     workflows and steps. Never invent step names.
  2. Split the operator's input into atomic obligations; merge restatements.
     For each: search existing rules by ID and by wording (the `rules --json`
     summaries) and classify as new, an amendment of `<id>`, or a filter
     change of `<group>`.
  3. Decide `paths` only when the sentence names a file kind or directory;
     show what the glob matches (`git ls-files | grep`) and drop a glob that
     matches nothing, saying so.
  4. Decide placement from real filters: an existing group whose filters fit,
     else propose a new group with filters, else the step's own `rules:`
     list for a one-off.
  5. Rewrite each rule to one imperative sentence, concrete nouns, no
     hedging, rationale and example in the body; propose a `check` only when
     it is obvious (a one-line shell command, or an existing tool's config
     the rule plainly belongs to, listed in the store's `checks`); otherwise
     leave it for the verifier, which proposes an approach the operator
     confirms before anything is built.
  6. Present one confirmation block: per rule, ID, group, filters, glob and
     match count, sentence, `new` or `replaces <id>: <old sentence>`; for an
     amendment with an approved command, say whether it will promote the
     command first (default when meaning is unchanged) or let it be
     re-derived. Wait for the operator's confirmation.
  7. Write only through `wwdev rules add/edit/move/filter/promote`; then run
     `wwdev lint` and show the result and where the rule now applies
     (`wwdev rule <id>`).
  8. Variants: `/ww-rule split <file>` (one rule per bullet of a prose
     document, batch confirmation; used for
     `documentation/python-agent-rules.md`); `/ww-rule from-review`
     (turn findings in the last review or fix page of a task into rules).
- After the skill exists, a separate task uses it to split
  `documentation/python-agent-rules.md` into `rules/python/` groups filtered
  per step, and replaces the document with a generated overview or a pointer.
- Docs, changelog, tests: CLI commands (create, refuse overwrite, edit hash
  warning, move, filter, promote, validation refusal), skill installation
  offered once, `init --skills`.

## 5. Out of scope, on purpose

- No `scope` property on rules; no `on_failure` on checks; no `--rule` flag
  on `start`; no other VCS; no JSON `verifier` block (hints live on rules and
  groups); no executable allowlist (the two-stage approval is the safety);
  no automatic pruning or mutation of the store; no separate verification
  step in YAML; no per-viewer configuration of delivery.
- The review gate is the existing assess step; it gets rules like any step and
  needs no new feature.

## 6. Order of work inside a slice

1. Parse and models, with unit tests.
2. Validation and lint.
3. Plan compilation and codec, schema bump, compatibility test.
4. State fields, schema bump, unknown-field tolerance test.
5. Execution (change set, checker, completion flow), integration tests.
6. Rendering (page, fix page, artifact), markdown and JSON tests.
7. Docs, examples, changelog.

Each slice ends with the full test suite green and `wwdev lint` on this
repository's own configuration passing.
