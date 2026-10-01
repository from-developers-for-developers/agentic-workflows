# Examples

Each example is a complete `ww-agentic-workflows.yaml`, unless it says otherwise, and every
one is loaded and compiled by the test suite. They are ordered from the simplest
to the most involved and each one introduces a different control or behaviour.
Agent-facing text is deliberately short; in a real project the descriptions
carry the instructions your agents need.

Run any of them with:

```console
./ww discover
./ww start TASK-1 --workflow <name> --agent codex --requirements "<requirements>" --role manager
```

## 1. A linear workflow

The smallest useful workflow. Every step is plain agent work, the implicit
`init` step records the requirements first, and each step produces an artifact.

```yaml
workflows:
  - name: task
    description: Implement a small change end to end.
    steps:
      - develop: Implement the requested change.
      - test: Run the tests and fix what fails.
      - document: Update the documentation the change affects.
```

## 2. Hooks and reusable handlers

Handlers are defined once and attached to lifecycle phases as workflow hooks.
Global hooks apply to every workflow, a workflow's hooks to one workflow, and
step hooks to one step.
Automatic handlers, here `argv` commands, run by ww itself; the agent never
executes them. `assert` lists conditions the command's output must meet. `idempotent: true` says
that running the handler again is harmless, so when ww is interrupted while
it runs, the next `next` replays it instead of stopping for an operator
decision; leave it off a handler whose replay could do damage, such as a
publish or a commit.

```yaml
handlers:
  - name: lint
    description: Run the linters.
    argv: [ruff, check, src]
    idempotent: true
  - name: verify-clean
    argv: [printf, clean]
    assert:
      - equals: clean
  - name: announce
    description: Tell the team what changed.

hooks:
  before_start_workflow:
    - workflows: [task]
      handlers:
        - verify-clean: ~
  before_complete_workflow:
    - handlers:
        - announce: ~

workflows:
  - name: task
    hooks:
      before_start:
        - steps: [develop]
          handlers:
            - lint: ~
    steps:
      - develop: Implement the change.
        hooks:
          after_complete:
            - lint: ~
      - review: Review the change.
```

## 3. Shell commands with arguments, environment, and variables

Shell source never interpolates directly; data goes through `args` and `env`.
`variables` asks the agent for values that later automatic steps consume, and
`{{...}}` interpolates them. `artifact: false` skips the artifact for a step
whose result is only its variable. Every value ww provides itself lives under
`ww.`, such as `{{ww.task.id}}`.

```yaml
workflows:
  - name: release
    steps:
      - pick-version: Decide the next version number.
        artifact: false
        variables:
          - version: The next semantic version, for example 1.4.0.
      - tag:
        shell: 'git tag -a "v$1" -m "$MESSAGE"'
        args: ["{{version}}"]
        env:
          MESSAGE: "Release {{version}} for {{ww.task.id}}"
      - notes: Write the release notes for {{version}}.
```

## 4. Modes, profiles, and execution settings

Modes are selectable guidance, profiles describe how an agent should behave,
and `agent`, `model`, and `reasoning` are advisory requests the manager sees in
the `auto` runtime. `role: manager` keeps a step in the managing session.

```yaml
modes:
  - economy: Use as few tokens as possible and keep artifacts short.
  - thorough: Prefer completeness over speed; verify every claim.

profiles:
  developer: Prefer small, well-tested changes and explain trade-offs briefly.
  reviewer: Look for defects and missing tests; do not restyle code.

workflows:
  - name: feature
    modes: [economy]
    profile: developer
    model: opus
    reasoning: high
    steps:
      - plan: Outline the change before touching code.
        role: manager
      - implement: Implement the plan.
      - review: Review the implementation.
        profile: reviewer
        agent: claudecode
```

## 5. Skills, slash commands, and MCP actions

A step can require a discovered agent skill or slash command instead of plain
prompt text (`kind: skill` or `kind: slash_command`), or address an MCP
connection. The examples below need a
`review-code` skill and a `ship` slash command in the agent's directory.

```yaml
workflows:
  - name: ship
    steps:
      - review-code: Review the change with the project's review skill.
        kind: skill
      - create-ticket: Create the release ticket and record its key.
        mcp: jira
        variables:
          - ticket: The key of the created ticket.
      - ship: Run the release command.
        kind: slash_command
```

## 6. Nested steps and artifact dependencies

`steps` groups related work under a parent step that becomes in progress with
its first child and completes with its last. `artifact_from` hands an earlier
step's artifact to a later one: a sibling at the same nesting level, or an
earlier step of an enclosing level, as `write-migration` does with `analyze`.

```yaml
workflows:
  - name: migration
    steps:
      - analyze: Analyze the current schema and list the required changes.
      - implement:
        steps:
          - write-migration: Write the migration.
            artifact_from: analyze
          - adapt-code: Adapt the code that reads the changed tables.
            artifact_from: write-migration
      - verify: Run the migration against a scratch database.
        artifact_from: analyze
```

## 7. Loops with break and continue

A `loop` repeats its body until a worker breaks it or the round limit is
reached. `break` and `continue` are natural-language conditions the worker
evaluates after doing the step. `max_rounds` overrides the project default, `limits.rounds` in
`ww-agentic-workflows.json`.

```yaml
workflows:
  - name: review-and-fix
    steps:
      - implement: Implement the change.
      - polish:
        max_rounds: 4
        loop:
          - review: Review the current state of the change.
            break: There are no meaningful findings left.
          - triage: Decide whether the findings are worth fixing now.
            continue: The findings are cosmetic and can be batched into the next review.
          - fix: Fix the findings.
```

## 8. Assessments

An assessment asks the agent for a decision and selects a subtree by its named
outcome. The compact form only continues or finishes the workflow. A workflow
holds at most one `assess` step, because the name is reserved.

```yaml
handlers:
  - name: refactor
    description: Refactor the modules the assessment named.

workflows:
  - name: maintenance
    steps:
      - assess:
          question: Does the recent development warrant refactoring?
          outcomes:
            positive:
              handler: refactor
            negative:
              steps:
                - record: Record that no refactoring is needed now.
            mixed:
              steps:
                - investigate: Gather the missing evidence.
                - decide: Decide and record the outcome.

  - name: follow-ups
    steps:
      - assess: Are there follow-up tasks worth opening?
      - open-follow-ups: Open the follow-up tasks.
```

## 9. Items: split work into pieces

An `items` step collects work items, here review findings, and then runs stages
for each of them. The bare form gets one built-in stage per item. The string
form gives splitting guidance. `assignment: per_item` keeps one worker for
all stages of an item in the `auto` runtime, and `item_phase` names the
standard item fields a stage fills.

```yaml
workflows:
  - name: quick-fixes
    steps:
      - collect: Review the pull request.
        items: One item per unresolved review thread; use the thread ID as the item ID.

  - name: review-feedback
    steps:
      - collect: Review the pull request.
        items:
          description: One item per review finding.
          assignment: per_item
          model: sonnet
          steps:
            - analyze: Analyze this finding.
              item_phase: analyze
            - fix: Resolve this finding.
              item_phase: resolve
            - reply: Reply in the finding's thread and resolve it.
              item_phase: report
```

## 10. A handoff workflow that chooses the next one

A workflow that ends in a transition step, `handoff_to` beside the step name,
hands off to another workflow, which continues as the next run of the same
task; the transition alone makes it a handoff workflow. This is how one entry
point routes a request to the right process.

```yaml
workflows:
  - name: route
    steps:
      - classify: Decide whether this request is a bug fix or a feature.
        artifact: false
        variables:
          - workflow: One of the workflows listed in {{ww.task.workflows}}, other than route.
      - route: ~
        handoff_to: "{{workflow}}"

  - name: bugfix
    steps:
      - reproduce: Reproduce the bug.
      - fix: Fix it and add a regression test.

  - name: feature
    steps:
      - implement: Implement the feature.
      - test: Test it.
```

## 11. Parent and child tasks

A `children` step collects independent pieces of work and runs a workflow for
each as its own task under the parent. Children run one at a time; the parent
continues when the last child completes. Until a child starts, `update-child`
can still change its text or project.

```yaml
workflows:
  - name: epic
    steps:
      - split: Split the epic into independent stories.
        children:
          description: One child per story a user would notice.
          workflow: story
      - summarize: Summarize what the stories delivered.

  - name: story
    steps:
      - implement: Implement this story.
      - test: Test it.
```

When the parent has work of its own around each child, list its stages under
`children.steps` instead of naming one workflow. The parent runs them once per
child, one child at a time; the stage with `workflow:` runs the child and waits
for it, and its artifact is the child's summary. Here the parent refines each
story before it starts, reviews it afterwards, and a `break` on the review ends
the epic early: the stories not started yet are skipped.

```yaml
workflows:
  - name: epic
    steps:
      - split: Split the epic into independent stories.
        children:
          description: One child per story a user would notice.
          steps:
            - refine: Sharpen {{ww.child.text}} with what earlier stories taught.
              role: manager
            - implement:
                workflow: story
            - review: Check that {{ww.child.id}} delivered what it promised.
              artifact_from: implement
              break: The epic is complete; no remaining story is worth building.
      - summarize: Summarize what the stories delivered.

  - name: story
    steps:
      - implement: Implement this story.
      - test: Test it.
```

## 12. Children that bind their own Jira IDs

When the child workflow's first step declares the variable `task_id`, each child obtains its
own external ID from that step when it starts. The parent's collection step
tells the agent not to pass `--id`. The same first step lets the parent itself
get its ID when started without one.

```yaml
workflows:
  - name: epic
    steps:
      - create-epic: Create the Jira epic and return its key.
        mcp: jira
        variables:
          - task_id: The epic key returned by Jira.
      - split: Split the epic into stories.
        children:
          workflow: story

  - name: story
    steps:
      - create-story: Create the Jira story for this child and return its key.
        mcp: jira
        variables:
          - task_id: The story key returned by Jira.
      - implement: Implement {{ww.task.id}}.
```

## 13. Saved metadata and project-scoped values

`saves` persists values an agent produces. A `metadata.<path>` entry stays with
the task; a `project_metadata.<path>` entry is shared by every task and read
back through `{{ww.project_metadata.<path>}}`. The agent passes each as
`--metadata <path>=<value>`, a project one as
`--metadata project_metadata.<path>=<value>`.

```yaml
workflows:
  - name: dependency-update
    steps:
      - update: Update the dependencies and note the highest risk change.
        saves:
          - metadata.dependencies.riskiest_change: The dependency whose update is most likely to break something.
          - project_metadata.dependencies.last_update: Today's date in YYYY-MM-DD format.
      - verify: Pay special attention to {{ww.metadata.dependencies.riskiest_change}}.
```

## 14. Git branches, commits, and worktrees

Git integration is the bundled `ww/git` extension. Its handlers are referenced
like any other, and its settings live in `ww-agentic-workflows.json`.

```yaml
hooks:
  before_start_workflow:
    - handlers:
        - ext/ww/git/handlers:is-git-clean: ~
        - ext/ww/git/handlers:start-task-branch: ~
        - ext/ww/git/handlers:create-worktree: ~
  before_complete_workflow:
    - handlers:
        - ext/ww/git/handlers:git-commit: ~
        - ext/ww/git/handlers:remove-task-worktree: ~

workflows:
  - name: task
    modes: [ext/ww/git/modes:conventional-commits]
    steps:
      - develop: Implement the change in the task worktree.
      - test: Run the tests.
```

```json
{
  "enabled": true,
  "limits": {"rounds": 3, "fixes": 3},
  "extensions": {
    "ww/git": {
      "commit_format": "{{ww.task.id}}: {{commit_message}}",
      "base_branches": {"default": "main", "hotfix": "release"},
      "separate_branch": true,
      "branch_name_formats": {
        "default": "feature/{{ww.task.id}}",
        "hotfix": "hotfix/{{ww.task.id}}"
      },
      "worktrees": true,
      "worktree_dir": "./ww-worktrees",
      "worktree_name_format": "{{ww.task.id}}"
    }
  }
}
```

## 15. One ww instance over several repositories

With `projects` in `ww-agentic-workflows.json`, the ww root is a workspace above
the repositories. `start --project` and `add-child --project` choose where a
task works, and the git extension follows.

```json
{
  "enabled": true,
  "projects": [
    {"name": "backend", "path": "./backend", "description": "Python API service."},
    {"name": "frontend", "path": "./frontend", "description": "React web client."}
  ],
  "extensions": {
    "ww/git": {
      "separate_branch": true,
      "base_branches": {"default": "main"},
      "branch_name_formats": {"default": "feature/{{ww.task.id}}"}
    }
  }
}
```

A repository with conventions of its own states them in its own
`ww-agentic-workflows.json`; only its `extensions` section is read, key by
key over the root's, so `frontend/ww-agentic-workflows.json` needs nothing
but what differs:

```json
{
  "extensions": {
    "ww/git": {
      "base_branches": {"default": "master"},
      "commit_format": "[{{ww.task.id}}] {{commit_message}}"
    }
  }
}
```

```yaml
hooks:
  before_start_workflow:
    - workflows: [feature]
      handlers:
        - ext/ww/git/handlers:is-git-clean: ~
        - ext/ww/git/handlers:start-task-branch: ~
  before_complete_workflow:
    - workflows: [feature]
      handlers:
        - ext/ww/git/handlers:git-commit: ~
        - ext/ww/git/handlers:return-to-base-branch: ~

workflows:
  - name: change
    description: A change that may touch several repositories.
    steps:
      - split: Split the change into one child per repository.
        children:
          workflow: feature

  - name: feature
    steps:
      - develop: Implement this part in {{ww.project.dir}}.
      - test: Run this repository's tests.
```

```console
./ww start CHANGE-1 --workflow change --agent codex --requirements "..." --role manager
./ww add-child CHANGE-1 --id api --text "API part" --project backend
./ww add-child CHANGE-1 --id web --text "Web part" --project frontend
```

## 16. A copied workflow, an early stop, and a recommended successor

`bugfix` is `hotfix` under another name, so `ww/git` gives it its own branch
format and base branch. `hotfix` recommends `merge-to-dev` when it completes,
and the operator confirms before it starts. The merge's assessment stops the
workflow outright when nothing needs a second look.

```json
{
  "extensions": {
    "ww/git": {
      "separate_branch": true,
      "base_branches": {"default": "main", "bugfix": "dev"},
      "branch_name_formats": {
        "default": "feature/{{ww.task.id}}",
        "hotfix": "hotfix/{{ww.task.id}}",
        "bugfix": "bugfix/{{ww.task.id}}"
      }
    }
  }
}
```

```yaml
hooks:
  before_start_workflow:
    - workflows: [hotfix]
      handlers:
        - ext/ww/git/handlers:start-task-branch: ~

workflows:
  - hotfix: Fix a bug on main.
    recommended_next_workflow: merge-to-dev
    steps:
      - investigate: Find the cause.
      - fix: Fix it.

  - bugfix: Fix a bug on dev.
    inherit: hotfix
    recommended_next_workflow: ~

  - merge-to-dev: Merge the task's branch into dev.
    steps:
      - merge: Merge the branch into dev and resolve any conflicts.
      - assess:
          question: Were conflicts resolved in non-trivial code?
          outcomes:
            positive:
              steps:
                - review: Review each resolution against both branches.
            negative:
              stop_workflow: true
      - verify: Run the tests and fix what fails.
```

The `start-task-branch` hook is written for `hotfix` and also runs for
`bugfix`, which clears the recommendation it would otherwise inherit.

## 17. Rules, checks, and the fix loop

Rules are sentences a step's agent follows. `develop` receives the
`engineering` group, its own two rules, and a `pytest` hook that sends the step
back to the worker when it fails, instead of stopping for the operator. Each
rule file is Markdown; its first sentence is shown on the step page.

```markdown
<!-- rules/python/no-print.md -->
---
paths: ["src/**/*.py"]
check:
  shell: grep -l 'print(' $WW_STEP_CHANGED_FILES || true
  assert: [empty]
---
Log through the `logging` module; never call `print` in library code.

Scripts under `bin/` may print; they are not library code.
```

```markdown
<!-- rules/python/contracts.md -->
State the observable behaviour being changed and the invariants that must hold.
```

```yaml
rules:
  engineering:
    rules: [rules/python/]
    workflows: [task]
    steps: [develop, refactor]

workflows:
  - name: task
    steps:
      - name: develop
        description: Implement the change with tests.
        rules:
          - Keep the public CLI unchanged.
          - text: Leave no TODO in the files you change.
            shell: grep -l TODO $WW_STEP_CHANGED_FILES || true
            assert: [empty]
        hooks:
          before_complete:
            - argv: [pytest, -q]
              on_failure: fix
      - name: refactor
        description: Simplify what develop wrote.
      - review: Review the change.
```

```json
{ "limits": { "fixes": 3 } }
```

When a check fails, `complete` exits non-zero and shows which checks failed
and what they printed; the step stays with its worker. After three rejected
completions ww stops with `operator_reason: fix_limit`: `next --retry` gives
the worker another round, `next --force --reason` waives the checks.

The rules without a command, "Keep the public CLI unchanged." and the
engineering rules, go to a verifier once develop's checks pass. The first time
it sees a wording it proposes how to check it; the operator approves with
`next --approve`, the verifier prepares the command, and after a second
approval ww runs it for that wording in every later step. Until then, and for
a rule no command can check, the verifier gives a verdict, and a failing one
sends develop back like a failed check.
