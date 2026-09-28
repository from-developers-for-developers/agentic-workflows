# Contributing

Thanks for wanting to help. ww is in beta and moving quickly, so the most
useful contributions are small, focused, and accompanied by the reasoning
behind them.

Everyone taking part is expected to follow the
[Code of Conduct](CODE_OF_CONDUCT.md).

## How to propose a change

1. **Open an issue first** for anything beyond an obvious fix — a typo, a
   broken link, a one-line correction. A short discussion before the code
   saves the larger rewrites. Use the
   [bug report](https://github.com/from-developers-for-developers/agentic-workflows/issues/new?template=bug_report.yml)
   or [feature request](https://github.com/from-developers-for-developers/agentic-workflows/issues/new?template=feature_request.yml)
   template. Report a security vulnerability privately, through
   [SECURITY.md](SECURITY.md), never as a public issue.
2. **Branch from `main`, and open the pull request against `main`.** It is
   the branch users run, so it is the base every change starts from. `dev`
   carries the maintainers' in-flight work and is not an integration branch
   for contributions — do not branch from it or target it. See the branch
   table in [README.md](README.md#releases-and-branches).
3. **Keep one change per pull request.** An unrelated refactor in the same
   branch makes the real change harder to review and harder to revert.
4. **Enable the repository's Git hook** so staged sources get their SPDX
   license identifier automatically:

   ```console
   git config core.hooksPath .githooks
   ```

5. **Run the checks** relevant to your change, and the complete set for
   anything release-facing. They are listed under
   [Run the checks](#run-the-checks).
6. **Record it.** If the change is user-visible, add an entry to
   `CHANGELOG.md` under a `## YYYY-MM-DD` heading for the day it lands,
   creating that heading if it is not there yet — merging to `main` puts it
   in front of users straight away, so nothing waits for a release. Keep the
   entry to a line or two: it is also what ww shows people in the terminal
   when it tells them an update is available. Update the affected documents
   under `documentation/` if behaviour or `ww-agentic-workflows.yaml` changed. A change to a
   persisted format needs a schema bump and must be called out as
   incompatible.

The pull request template asks for the same things; filling it in is the
fastest way to a review.

Two documents are worth reading before a substantial change:
[python-agent-rules.md](documentation/python-agent-rules.md), the engineering
rules this codebase is held to — parse/plan/execute separation, abstraction
choice, persistence invariants — and [todo.md](documentation/todo.md), the
known incomplete boundaries, so you do not rediscover one as a bug.

Before 1.0, changes that alter persisted formats or documented contracts are
acceptable when they are deliberate and documented. Changes that make them
alter *silently* are not.

## Set up a development environment

Create a virtual environment from the repository root and install the project
with its development dependencies:

```console
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e '.[dev]'
```

The project supports Python 3.10 and newer. Run all commands below from the
repository root. Development and CI support POSIX systems (Linux and macOS).
Native Windows is not supported because filesystem locking relies on POSIX
`fcntl`; use WSL or another POSIX-compatible environment on Windows.

## Run the checks

Run the test suite:

```console
.venv/bin/python -m pytest
```

Run lint checks over the Python sources, tests, and scripts, as well as the
separately bundled Git extension:

```console
.venv/bin/python -m ruff check src tests scripts
.venv/bin/python -m ruff check ext/ww/git
```

Run the configured static type checks:

```console
.venv/bin/python -m mypy
```

Run the focused recovery and external-storage-adapter release gates:

```console
.venv/bin/python scripts/check_release_gates.py
```

Build and validate the distributable artifacts. This checks that the sdist and
wheel contain the license and runtime resources, then installs the wheel in a
clean virtual environment outside this checkout:

```console
.venv/bin/python scripts/check_distribution.py
```

Please run the checks relevant to your change before submitting it. For a
release-facing change, run the complete set above.

## Add an internal workflow action

Keep the action's typed payload and implementation together in a module under
`src/ww/actions/`. Register it in
`src/ww/actions/__init__.py`. The implementation owns parsing, validation,
planning, instruction content, and plain-data payload encoding. Keep `provide`,
`update_metadata`, execution settings, hooks, artifacts, and state transitions in
core. Add a test that parses and compiles the action, saves and loads the plan,
and renders its instruction. See the
[architecture guide](documentation/architecture.md) for the registry and phase
contexts. Keep a new payload beside its action implementation; do not reuse a
built-in payload merely to enter a generic consumer. Start contract coverage
with [the contract helper](tests/action_contract_helpers.py) and
[the example action](tests/unit/test_action_example.py), then add capability tests for automatic,
recovery, or definition-override behavior the action opts into. Workflow
structure is not an action extension point: add a core operation deliberately
when changing loops, handoffs, or child-workflow coordination.
Subclass `Action` for agent-owned work and `AutomaticAction` for work ww runs
itself. The abstract lifecycle methods are required; the hooks below have safe
defaults and are overridden only when the action has that behaviour.

| Behavior | Where it lives |
| --- | --- |
| Automatic effects | `AutomaticAction.execute` through the narrow `ExecutionContext` services |
| Validation before execution | `AutomaticAction.preflight` with identity-only services |
| Automatic recovery checks | `AutomaticAction.check_recovery` returning a `RecoveryCheckResult`, or `None` for no checker |
| Durable command segments, permissions, output attestation, manual attestation, artifact attribution, extension binding | `Action.traits(planned)` returning an `ActionTraits` value |
| Handler payload inheritance | `Action.override_definition` |

Registration checks that the class subclasses `Action`, that owner and
execution agree, and that automatic actions subclass `AutomaticAction`. Core operations
are planned and dispatched directly; actions do not receive the workflow
service or mutable execution state. Command-segment
attestations accept stdout only; values/workspace apply at whole-action scope.
Keep action identifiers stable and retain decoding support for saved payloads
when changing an action. Missing implementations leave saved payloads readable,
but execution requires their implementation to be registered again.

[Retained plan fixtures](tests/fixtures/action_plan_compatibility.json) pin
compiled plan ordering, identities, and the persisted `operation` format at the
current plan schema. Regenerate them only together with a deliberate format
change and a `PLAN_SCHEMA_VERSION` bump, never merely to make a refactor pass.
Add capability-specific integration cases alongside the basic contract helper,
including save/reload and interrupted-operation behavior.

## Add an internal composite planner

Use `src/ww/plan/constructs.py` when a normalized `StepDefinition` needs to
expand into a nested region or control boundary rather than one ordinary action.
Define a small typed input, implement `expand`, and register it with the
internal construct registry. Planners may request shared lifecycle compilation,
scoped annotations, leaf emission, and typed boundaries; they must not access
the compiler, mutable item collection, task state, or storage. Keep YAML
parsing/validation and all generic lifecycle policy in their existing owners.
Add a focused registry test plus plan-equivalence coverage for the construct's
supported nesting and saved-plan behavior.
The registry supports normalized internal `StepDefinition` shapes only. It is
not a public YAML extension surface: a new YAML construct spelling, arbitrary
nested shape, or scheduling primitive needs explicit parser and core work.

## Licensing

By contributing to this project, you agree that your contributions
will be licensed under the GNU General Public License v3.0 or later
(GPL-3.0-or-later).
