# ww - agentic workflows

`ww-agentic-workflows` is a local-first CLI for defining and running repeatable
workflows with coding agents. It compiles `ww.yaml` into an explicit plan,
separates work performed by the agent from automation performed by `ww`, and
saves task state so work can be inspected and resumed.

Use it when a development process needs more structure than a prompt: workflow
hooks, reusable handlers, worker-stoppable review/fix loops, MCP-backed
actions, saved artifacts, and a durable record of what ran.

Website: <https://agenticworkflows.dev>

> **ww is in beta.** `ww.yaml` syntax, task state, and commands may
> still change; [documentation/limitations.md](documentation/limitations.md)
> says what is not promised yet. `ww-agentic-workflows --version` and `init`
> say so too.

## See it run

`scripts/demo.sh` builds a throwaway project in a temporary directory,
initializes ww in it, writes a two-step workflow with a test handler, and runs
one task from start to completion. Nothing outside that directory is touched:

```console
scripts/demo.sh
```

Abridged, it looks like this. The agent runs these commands; ww answers each
one with the next:

```console
$ ./ww start TASK-1 --workflow task --agent claudecode \
    --requirements "Add retry handling to the upload client." --role manager

# TASK-1 · task
## Manager and worker: dispatch the `develop` assignment
### Manager command
To continue the workflow, run:
    ./ww next TASK-1 --role manager

$ ./ww next TASK-1 --role manager

# TASK-1 · task
## Manager and worker: perform `develop`
### Task requirements
Add retry handling to the upload client.
### Work instruction
Implement the requested change.
### Next steps
- document
### Worker completion command
    ./ww complete TASK-1 --role worker --artifact="<whole result in Markdown>" \
        --summary="<one or two sentences for the next step>"

$ ./ww complete TASK-1 --role worker \
    --artifact "Added exponential backoff to UploadClient.send." \
    --summary "Retries land in UploadClient.send; document the backoff settings."

# TASK-1 · task
> Completion recorded successfully by `ww`.
## Manager and worker: perform `document`
### Previous step result
The `develop` step left this summary for you:
> Retries land in UploadClient.send; document the backoff settings.
...
```

The test handler in that workflow ran inside the `complete` — ww executed it
itself, and the agent never saw it as work of its own.

## Beta

ww is in beta and under active development. Expect rough edges, and expect to
hit them: unclear instructions to your agent, a handler that fails in a way ww
does not yet explain well, a workflow shape that does not compile.

Please report them at
[GitHub Issues](https://github.com/from-developers-for-developers/agentic-workflows/issues).
A report is most useful with all four of these:

1. **The workflow.** The relevant part of your `ww.yaml`, reduced to
   what still reproduces the problem.
2. **The command you ran**, its exit code, and the exact message ww printed.
3. **What you expected instead.**
4. **Your agent's own account of what went wrong.** If you hit this while
   working through an agent, ask it to summarise what it ran, what ww told it,
   and where it got stuck, and paste that. It is often the fastest route to the
   real cause.

Sanitise before pasting: `.ww/` and command output may contain credentials or
other private values. See [Sensitive runtime data](#sensitive-runtime-data) below.

Two things worth reading before you rely on it:
[Stability and compatibility](#stability-and-compatibility), for what is
settled and what is not, and
[documentation/limitations.md](documentation/limitations.md), which lists what
ww deliberately does not do yet so you can tell a limitation from a bug.

## Getting started

### 1. Install

Python 3.10 or newer and [pipx](https://pipx.pypa.io/) are required. Install
ww from PyPI:

```console
pipx install --pip-args=--pre ww-agentic-workflows
ww-agentic-workflows --version
```

The package is `ww-agentic-workflows`. Until a stable release exists, PyPI
holds development snapshots, `1.0.0.devN`, which `--pre` selects. Each one is
built from `dev` after the automated checks pass; see
[Releases and branches](#releases-and-branches). To move to the newest
snapshot later:

```console
ww-agentic-workflows upgrade
```

To keep a second install beside the first, give it its own global name with
pipx's `--suffix`, for example
`pipx install --suffix=-dev --pip-args=--pre ww-agentic-workflows`, and set
`"executable": "ww-agentic-workflows-dev"` in the `ww.json` of each project
that should use it. ww then prints that binary in every command, and the
project's `./ww` runs it. [Development releases](documentation/development-releases.md#a-development-channel-beside-a-stable-one)
covers this setup.

#### From a source checkout

To run `main` directly, or to work on ww itself, install a clone in editable
mode instead. The package name is the same, so remove a PyPI install first
with `pipx uninstall ww-agentic-workflows`, or give the checkout a suffix:

```console
mkdir -p ~/tools
git clone https://github.com/from-developers-for-developers/agentic-workflows.git \
  ~/tools/agentic-workflows
pipx install --editable ~/tools/agentic-workflows
```

Editable installation keeps the command connected to that checkout; after a
`git pull` in `~/tools/agentic-workflows`, the new code is live with no
reinstall, and `ww-agentic-workflows upgrade` performs that pull for you.
[CONTRIBUTING.md](CONTRIBUTING.md) describes the development environment for
changing ww.

### 2. Initialize your project

Run `init` from the root of the project you want to run workflows in:

```console
cd /path/to/your-project
ww-agentic-workflows init
```

The wizard asks a few questions and then sets the project up:

- **`ww.yaml`** — an empty, structured file for you to fill in.
- **`./ww`** — a small launcher, so every later command is `./ww <command>`
  from the project root regardless of where you are installed.
- **`WW_AGENT_INSTRUCTIONS.md`** — the instructions your agents read.
- **`ww.json`** — project configuration: task ID format,
  enabled extensions, base branches, optional multi-repository `projects`.
- **Git setup**, in a Git repository: it enables the bundled `ww/git`
  extension and asks about worktrees and branch formats, and offers to keep
  `.ww/` out of Git (`.ww/*` in `.gitignore`), except the files where ww
  records what it learned about the project (`.ww/project.md`), which is
  meant to be committed. It also keeps local configuration files
  (`*ww.local.yaml`, `*ww.local.json`,
  `ww-setup.local.yaml`) out of Git.
- **Agent skills** — for each agent directory it finds (`.claude/`, `.codex/`
  and so on), it offers to install a `ww` skill, so you can ask the agent to
  work through ww by name, a `noww` skill, so you can tell it to leave ww
  out, a `ww-rule` skill, which turns your own words into rules for ww's
  steps, and the `ww-setup` skill with the skills it guides through
  (`ww-learn-project`, `ww-suggest`, `ww-refresh`, `ww-solve`,
  `ww-rules-from-artifacts`, `ww-automate`, `ww-scriptize`), a `ww-wizard`
  skill that helps you create or change a workflow, improve rules or pick an
  approach (changes to an existing workflow go through `ww setup update`), and
  `ww-deduce-feedback` and `ww-feedback-rules` for learning from completed
  artifacts and proposing rules.
- **Your user configuration directory**, `~/.config/ww/`,
  where settings of your own for every project live.

For Claude Code it can also write, if you opt in, Bash allow rules for ww's role
commands (through the project wrapper's absolute path) into
`.claude/settings.local.json`, keeping that file out of Git; `init --permissions`
answers the question yes, as described in [features](documentation/features.md).

It finishes by printing any manual additions you still need in `AGENTS.md` or
`CLAUDE.md`, the exact permission entries that let your agents run ww without
asking each time (for Claude Code, the lines to add to
`.claude/settings.json`), a reminder to define a workflow, and the next step:
run the `ww-setup` skill to set ww up for this project.
That skill learns the repository (what it is for, its stack, commands, CI,
review and release process, conventions and recurring pitfalls), asks a few
questions about your process (express setup skips them and derives defaults
from the project), and proposes a minimal setup shaped by the project's own
branches, commands and history, each piece with the evidence for it, which
you try alone first and share with the team if you like. It never interviews
you about who you are, your role, your team or your company. Each part is optional and shows you every change before ww places it; see [Setting ww up](documentation/features.md#setting-ww-up-learning-and-suggestions).

`init` takes flags for every prompt if you would rather not answer them
interactively — `--no-input` accepts all defaults, and
`ww-agentic-workflows init --help` lists the rest.

Running `init` again is safe. It restores missing ww-owned files and
directories and leaves your own content alone. `init --force` asks every
question again, ignoring your saved answers, so you can add agents, skills or
hooks later; it only ever adds.

### 3. Define a workflow

Open `ww.yaml` and describe the process as a list of steps. The
smallest useful workflow is all agent work:

```yaml
workflows:
  - name: task
    description: Implement a small change end to end.
    steps:
      - develop: Implement the requested change.
      - test: Run the tests and fix what fails.
      - document: Update the documentation the change affects.
```

Every workflow also gets an implicit first `init` step, which records the
requirements for the whole task.

As the configuration grows, `ww.yaml` can split its definitions across
other YAML files it lists under `imports`; see
[the features guide](documentation/features.md#split-wwyaml-into-several-files).
Both files can also be extended for you alone, across all your projects, in
`~/.config/ww/ww.yaml` and `.json` (a
directory `init` creates), and per checkout, in `ww.local.yaml`
and `.json` next to the repo files; see [user, repo, and local
configuration](documentation/features.md#user-repo-and-local-configuration).

Check it before you run anything:

```console
./ww lint
./ww plan --workflow task --agent claudecode
```

`lint` validates the file on its own. `plan` compiles one workflow into the
exact, ordered plan that would run — every step, hook, and handler — without
creating any task state.

[documentation/examples.md](documentation/examples.md) has twenty-one complete,
tested `ww.yaml` examples, from this one up to loops, per-item work,
child tasks, and Git integration.

### 4. Ask your agent to do the work

You do not drive ww by hand. You ask your coding agent for the work in the
usual way, and it runs ww for you:

> Implement retry handling in the upload client. Use ww.

`init` has already given the agent everything it needs to act on that:
`WW_AGENT_INSTRUCTIONS.md` tells it to route project work through ww, and the
installed `ww` skill lets you name the tool explicitly. From there the agent
starts at `./ww discover`, which tells it whether to use ww in this project
by default, only when you ask for it, or not at all, and lists the project's workflows,
modes, and the exact commands to start and resume work. It picks the workflow matching your request and opens the
task:

```console
./ww start TASK-123 --workflow task --agent claudecode \
  --requirements "Add retry handling to the upload client." --role manager
```

That command is the agent's, not yours — but it is worth being able to read
one:

- `TASK-123` is the task ID. When your request names a ticket, the agent uses
  that key, so ww's task matches the issue. Otherwise ww assigns one in the
  format chosen during `init`.
- `--agent` is the agent integration in use: `codex`, `claudecode`, `gemini`,
  `antigravity`, `deepseek`, `kimi`, `cursor`, `grok`, or `custom:<name>`.
- `--requirements` is your request, normalized into the task's requirements.
- `--role manager` is the role driving the task; a worker performing a single
  assignment uses `--role worker`.

A request no workflow fits still goes through ww once it changes files. The
agent records it with `catchall`, a one-step workflow ww provides to every
project, and otherwise works exactly as it would without ww. Questions and
other read-only work never start a task. The agent first runs `./ww lookup`
with the task you named, however you wrote it (`12345` finds `FOOBAR-12345`),
and a task ww has never seen is only created after you confirm it in the
agent's choice menu. Say `/noww` when you want the
agent to leave ww out.

If you would rather open the task yourself — to pin an external ticket key, or
to hand a prepared task to an agent — the same command works typed in.

### 5. Let it run, and watch what it does

From there the agent works the task to its end. Every ww response finishes by
printing the exact next command, so the agent always knows what follows: `next`
opens a step and prints its instructions, the agent does that step, and
`complete` records the result and moves on.

```console
./ww next TASK-123 --role manager
./ww complete TASK-123 --role worker --artifact "What this step produced." \
  --summary "The short handover the next step needs."
```

One completion rarely finishes a task; the agent keeps going until ww reports
the workflow complete. Automatic handlers — your tests, linters, commits — run
inside `next` and `complete`, executed by ww itself, and the agent never runs
them or works around them.

Your part is to answer when asked and to look in when you want to. These are
the commands worth knowing:

```console
./ww status TASK-123        # compact: workflow, current step and state, runtime, agent
./ww artifacts TASK-123     # what each step produced
./ww instruction TASK-123 --role manager   # the full current instructions
```

`instruction` is also how work resumes: a new session, or a different agent,
picks up a task it did not start by reading it.

Two things will interrupt the agent and come back to you. An **interactive
step** is a question ww requires a human to answer — the agent asks it in your
own chat, or opens a local operator page for a per-item answer sheet, and the
task waits. An **automatic handler failing** — a test suite that will not pass,
a commit that is rejected — stops the task and reports the error, because
recovery is your decision, not the agent's: retry the handler, or force past it
with a recorded reason. A stop for an incomplete item pass (`pass_incomplete`)
is cleared by recording the missing values with `update-item` and then
`next --retry`; forcing is refused there.

If the agent's own work genuinely cannot be finished, it records that rather
than leaving the task open:

```console
./ww fail TASK-123 --role worker --error "<reason>"
```

## How it works

`ww` runs CLI handlers itself. The agent performs prompts, skills, slash
commands, and MCP actions, then reports their results with the exact `complete`
command shown by the CLI. One manager `next` dispatches a step lifecycle; the
worker completes its main action and its agent-owned workflow hooks until ww explicitly
hands control back. Each started workflow uses a saved plan, so later
configuration edits cannot alter work already in progress.

After a workflow finishes, ww suggests feedback deduction from artifacts of
steps marked `learnable: true`. The `ww-deduce-feedback` skill records
candidates with evidence, counters and encounter times. This is enabled by
default; set `"feedback_learning": false` in `ww.json` to disable it. Invoke
`/ww-feedback-rules` separately to review candidates, approve rules and prune
stale points. See [feedback learning](documentation/features.md#learning-from-operator-feedback)
for commands and examples.

### Runtimes: who actually performs a step

Every task runs manager and worker responsibilities — the manager dispatches
assignments and handles recovery, the worker performs one assignment and
reports it. A **runtime** decides where those two live. The plan and the
commands are identical either way; only the division of labour differs.

| Runtime | Who does the work |
| --- | --- |
| `single` (the default) | One session is both manager and worker and performs every assignment itself. No subagents. |
| `auto` | The manager delegates each assignment to a worker agent it selects, and records which one it used. |

Choose one with `--runtime` / `-r` on `start`. Omitted, ww takes the workflow's
own `runtime` if it declares one, then `"runtime"` in
`ww.json`, and falls back to `single`. The flag always wins.

This is what makes the next part meaningful. Workflows, steps, and handlers may
request an `agent`, `model`, and `reasoning` — always advisory, never binding.
In `auto` the manager reads them when picking a worker, and records the worker
it actually chose. In `single` there is nobody to delegate to, so ww keeps the
requests on the saved plan for inspection and otherwise ignores them: your own
session's settings are the ones in force.

An optional `projects` list in `ww.json` lets one ww instance
coordinate tasks and child tasks across several repositories, each working in
its own directory. Set `"enabled": false` in `ww.json` to tell
agents not to use ww in a project at all.

## Releases and branches

Every push to `dev` publishes a development snapshot to PyPI as
`ww-agentic-workflows` `1.0.0.devN`, after the automated checks pass. Snapshots
are for using and testing unreleased changes; install them as shown in
[Install](#1-install). A source installation, `pipx install --editable` from a
clone, follows whichever branch the checkout tracks.

See [development releases](documentation/development-releases.md) for
publishing setup and versioning. Beta and stable package publishing are not
configured yet. The branches serve these purposes:

| Branch | Use it for |
| --- | --- |
| `main` | What contributions branch from, and what a source checkout should track. It receives updates frequently. |
| `dev` | The maintainers' in-flight work, from which the PyPI development snapshots are built. Do not target it in a contribution pull request. |

To update either a source checkout or a package installation:

```console
ww-agentic-workflows upgrade
```

Source checkouts update their tracking branch with a fast-forward pull; local
changes are preserved by refusing to upgrade a dirty checkout. Package installs
upgrade through pip or pipx. Stable installs follow stable releases; development,
beta and RC installs also allow prereleases. `upgrade --pre` opts a stable
installation into prereleases.

WW checks for updates at most once a day and shows a short notice above normal
command output. Source installations compare Git commits; package installations
compare PyPI versions. Checks remain silent when offline. `upgrade` does not
wait for open tasks; skim [CHANGELOG.md](CHANGELOG.md) first, because a release
that changes the task state format can leave tasks in progress unreadable.

```console
ww-agentic-workflows updates            # show the cached notice
ww-agentic-workflows updates --now      # check now
```

Set `"update_check": false` in `ww.json` to switch notices off for a project,
or `WW_UPDATE_CHECK=0` to switch them off everywhere. See
[update notices and upgrading](documentation/features.md#update-notices-and-upgrading-ww)
for the installer behavior and task checks.

## Stability and compatibility

The shape of ww has largely settled. `ww.yaml` and the command surface
have been stable in practice for a while, and most work on `main` now is
internal refactoring, new capabilities, and fixes rather than changes to what
you have already written. Frequent updates do not mean frequent breakage.

Development snapshots can be pinned, but there is not yet a formal deprecation
cycle or a promise that an incompatible change could not land.
When one does, it is deliberate and rare, and
[CHANGELOG.md](CHANGELOG.md) marks it "Breaking:". A changed name is then an
unknown key that `lint` reports, and saved task state in another schema
version is refused rather than guessed at: finish or reset in-flight tasks
first.

| Surface | Where it stands |
| --- | --- |
| `ww.yaml` | Settled. Keys are added far more often than they change, and `lint` tells you immediately if something no longer parses. |
| The CLI | Settled for interactive use. Commands, flags, and printed text are still not a machine interface — use `--json` if a script depends on output. |
| Task state under `.ww/` | The volatile one. The on-disk format is versioned and the reader rejects any other version, so an unfinished task may not load after an upgrade. |
| The extension API | Documented and the most deliberate of these; changes are announced. |

Two habits cover almost everything:

- **Finish or `reset` in-flight tasks before you upgrade.** This is the one that
  actually bites people — a task started last week refusing to load this week.
  Completed tasks are unaffected.
- **Skim [CHANGELOG.md](CHANGELOG.md) when you update.** ww tells you when a
  newer version is available; for a source checkout it also shows the entries
  you would be pulling in.

If you want a fixed target anyway, pin a snapshot and move deliberately:
`pipx install --force ww-agentic-workflows==1.0.0.dev42`, or
`git -C ~/tools/agentic-workflows checkout <sha>` for a source checkout. Once
ww reaches a stable release, the package will carry the usual guarantees.
[documentation/limitations.md](documentation/limitations.md) has the detail,
alongside the execution model and the workflow shapes ww does not support.

## Supported platforms

ww supports Python 3.10 and newer on POSIX systems, including current Linux
and macOS releases. Its filesystem locking uses POSIX `fcntl`; native Windows
is not supported. Windows users can run ww in a POSIX-compatible environment
such as WSL.

Task state and saved plans are private runtime data, not a general-purpose
interchange format; do not edit them by hand. Extension authors should use
only the documented public extension API.

## Sensitive runtime data

Treat `.ww/` as private runtime data, apart from the shared learning file
`project.md`, which is meant for the repository. It can contain task errors, worker
artifacts, command stdout/stderr, metadata, and configured extension settings;
any of those may include credentials or other sensitive values supplied to a
workflow. The audit file `.ww/executions.jsonl` is owner-readable only and
records redacted invocations plus a stable error code, not arbitrary error
text. It is an audit aid, not a general secret-scrubbing system.

Do not send `.ww/`, task-state JSON, command-output files, or settings
snapshots in a support request by default. Start with the ww version, the
workflow name, a manually sanitized error summary, and the output of commands
that you have reviewed for sensitive values. If protected runtime detail is
needed, remove secrets first and share only the smallest relevant copy through
your approved channel. Prefer environment variables or a secret manager over
placing credentials in workflow configuration, metadata, or command arguments.

[SECURITY.md](SECURITY.md) describes what ww can do on your machine — where
the trust boundary sits, what runs your configuration, and what reaches the
network — and how to report a vulnerability privately rather than in a public
issue.

## Git branches and worktrees

The bundled Git extension accepts a project-wide base branch and
workflow-specific overrides in `ww.json`. A base can be a
literal branch name or an `argv` command whose single non-empty stdout line
names the branch:

```json
{
  "extensions": {
    "ww/git": {
      "base_branches": {
        "default": "main",
        "bugfix": "develop",
        "task": {"argv": ["./scripts/base-branch", "{{ww.task.workflow}}"]}
      }
    }
  }
}
```

An entry named after the workflow wins over `default`. Commands run directly,
without a shell, from the project root and may interpolate `{{ww.task.id}}`,
`{{ww.task.workflow}}`, and `{{ww.task.run}}`. Child tasks still branch from their recorded
parent branch.

## A larger workflow

This example combines reusable handlers, workflow hooks, a Jira MCP step,
agent instructions, structured CLI automation, and values passed between steps:

```yaml
handlers:
  - name: tests
    argv: [python, -m, pytest, -q]

  - name: stage
    argv: [git, add, .]

  - name: commit
    variables:
      - name: commit_message
        description: A concise commit message.
    argv: [git, commit, -m, "{{ww.task.id}}: {{commit_message}}"]

hooks:
  before_complete:
    - workflows: [jira-task]
      steps: [develop]
      handlers:
        - name: tests
        - name: stage
        - name: commit

workflows:
  - name: jira-task
    steps:
      - name: create-jira-issue
        mcp: jira
        description: Create a Jira issue for the requested work and return its key.
        variables:
          - name: jira_key
            description: The created Jira issue key.

      - name: develop
        description: Implement {{jira_key}} and verify the result.
```

`init` is a reserved first step that `ww` adds to every workflow, so it is not
listed under `steps`. The manager supplies it through `start --requirements`,
using corrected grammar and consistent styling without analysis or a work plan.
ww retains it durably while init preparation hooks run, then records it as the
normal artifact-producing init step. It can be targeted by hooks without
allowing later user-step inputs to be consumed during `start`.

Running it end to end looks like this:

```console
ww-agentic-workflows lint
ww-agentic-workflows plan --workflow jira-task --agent codex
ww-agentic-workflows start TASK-123 --workflow jira-task --agent codex \
  --requirements="Implement and verify the requested Jira-backed change." --role manager
ww-agentic-workflows next TASK-123 --role manager
ww-agentic-workflows complete TASK-123 --role worker --variable jira_key=PROJ-456 \
  --artifact="Created Jira issue PROJ-456."
ww-agentic-workflows next TASK-123 --role manager
ww-agentic-workflows complete TASK-123 \
  --role worker \
  --variable commit_message="Implement PROJ-456" \
  --artifact="Implemented and verified PROJ-456."
ww-agentic-workflows complete TASK-123 --role worker \
  --variable summary="Created and implemented PROJ-456."
```

## Documentation

- [specification.md](documentation/specification.md) is the exact `ww.yaml`
  specification: keys, value types, defaults, scopes and failure semantics.
- [features.md](documentation/features.md) is the design guide and feature
  reference: when to use what, with configuration and command examples.
- Both, with the examples below, are the authorities for designing workflows.
  An installed ww prints its own copies: `ww docs specification|features|examples`.
- [architecture.md](documentation/architecture.md) describes internals, boundaries, state, and
  design decisions.
- [agent-hooks.md](documentation/agent-hooks.md) covers the agent's own hooks
  (session-start, stop, interrupt): what they do, per-agent support, and
  install/uninstall/show.
- [examples.md](documentation/examples.md) is a set of complete, tested
  `ww.yaml` examples, one per control or behaviour.
- [limitations.md](documentation/limitations.md) lists what ww does not do yet, and
  the compatibility and platform boundaries to expect before 1.0.

## Development

For fresh-clone setup and the complete check list, see
[CONTRIBUTING.md](CONTRIBUTING.md). The release artifact check builds an sdist
and wheel, inspects their bundled resources and license, and installs the wheel
in an isolated virtual environment.

```console
scripts/test
.venv/bin/python scripts/check_release_gates.py
.venv/bin/python scripts/check_distribution.py
```

`scripts/test` creates or reuses `.venv` with a Python version supported by
`pyproject.toml`, repairs a partial development install, then runs ruff, mypy
and the test suite. Set `WW_PYTHON` to choose the interpreter for a new
environment. It preserves existing environments when setup fails.

Pushes to `dev` also publish CI-checked development snapshots to PyPI once
Trusted Publishing is configured. See
[development releases](documentation/development-releases.md) for versioning,
installation and the one-time setup.

## License

ww-agentic-workflows is free software licensed under the GNU General Public License v3.0 or later (GPL-3.0-or-later).

See [LICENSE](LICENSE) for the full license text.

### Use with other projects

Using ww-agentic-workflows to develop, build, test, review, or otherwise operate on another project does not by itself impose the GPL license on that project, its source code, its workflow configuration, or artifacts produced from the user's own content.
