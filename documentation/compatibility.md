# Compatibility

ww is in beta, and strict semantic versioning applies only from 1.0 (see
[limitations.md](limitations.md#distribution-and-versioning)). The machinery
that will keep that promise exists already: every persisted file says which
schema and which ww wrote it, and an upgraded ww migrates what an older one
wrote instead of refusing it. This page describes that policy and how ww
applies it.

## Version policy

A persisted format's schema version changes only on a backward-incompatible
change:

- a field removed or renamed;
- a field whose type or meaning changes;
- a value removed from a closed set, such as a status;
- a changed default. Task state omits fields at their default, so a new
  default would silently change what every older file says.

A field added with a default that keeps the old behaviour is additive and
never changes the version: a file written before it simply lacks the field,
and the reader takes the default. The same rule holds for
`EXTENSION_API_VERSION` and for an extension's major version: an additive
change is never a reason to raise either.

Every persisted file records the ww version that wrote it as `written_by`,
for example `"written_by": "1.0.0b18"`. It is informational: no digest or
comparison includes it, and a file written before ww recorded it reads the
same. A run nothing changed keeps its file, and the `written_by` it was
written with.

A file whose schema version is higher than this build reads is refused, with
the format, the writer and both versions:

```text
task state written by ww 1.2.0 (schema 2); this ww reads up to schema 1: upgrade ww
```

A higher schema version without `written_by` comes from a beta before the
schema versions were renumbered, not from a newer ww, and is refused as such:

```text
task state written by a beta ww from before the schema versions were renumbered (schema 2); finish or reset it with that ww
```

Plan snapshots and execution states are nested in a run's state file and
carry no `written_by` of their own. The file around them is checked first, so
a higher schema version in a nested record always means a newer ww wrote it.

Downgrading is not supported. An older ww refuses a newer schema, and it may
also refuse a file a newer ww wrote at the same schema version, when that
file holds a field the older ww does not know where unknown fields are
rejected (plan items, the rule automation store and the rule dispute log
reject them, to catch damaged files).

## Persisted data and load-time migrations

Each persisted format has a current version, an oldest version this build
still reads, and a chain of migrations between them, all declared in one
place, `ww.migrations.data`. A migration is a pure function from the mapping
at version N to the mapping at N + 1. Every reader hands the raw file to that
chain before decoding it, so the decoders and the engine only ever see the
current shape; writers always write the current version.

An open task is therefore upgraded on its next save; a closed task, which is
never saved again, is migrated each time it is read. A file older than the
oldest version a format still migrates is refused, naming the ww that wrote
it:

```text
task state written by ww 1.0.0b18 (schema 1); this ww reads schema 2 and later: finish or reset it with ww 1.0.0b18
```

Finish that task with the ww named, or reset it, before upgrading further.

| Format | Where | Version field |
| --- | --- | --- |
| task state | `.ww/tasks/<id>/state.json`, the task index | `schema_version` |
| run state | `.ww/tasks/<id>/runs/<run>/state.<revision>.json` | `schema_version` |
| plan snapshot | inside a run state, `run.snapshot` | `schema_version` |
| execution state | inside a run state, `run.state` | `schema_version` |
| rule automation store | `ww-rule-automation.json` at the project root | `schema_version` |
| rule dispute log | `.ww/rule-disputes.json` | `schema_version` |
| rule stats | `.ww/rules/stats.json` | `schema` |
| feedback store | `.ww/feedback.json` | `schema` |
| run report | `.ww/debug/<id>.json`, `.ww/feedback/<id>.json` | `schema` |

Every format is at schema 1 except the plan snapshot, at schema 2: its plan
items' `artifact_dependency` became the list `artifact_dependencies` when
`artifact_from` began to accept several steps, and a snapshot at schema 1 is
migrated as it is decoded (`ww.migrations.plan_snapshot`). The task index and
the run documents are separate formats, versioned independently, because a
commit rewrites only the runs whose content changed: an index can be newer
than a run file beside it. A run's plan snapshot and execution state carry
their own versions and migrate when they are decoded; they have no
`written_by` of their own, since they are written with their run file.

Rule stats are counters that never block work: a file ww cannot read counts
as empty. One a newer ww wrote is left as it is rather than replaced, so its
counts survive until that ww reads them again.

Other files under `.ww/`, such as task metadata, amendments, shared items,
bootstrap requests and the audit log, carry no schema version.

`tests/fixtures/formats/<format>/v<N>.json` keeps a real file of each format
at each version, and a test loads every one with the current code. A sample
is never edited or removed while its version is readable, so that test fails
only when a file an earlier ww wrote stops loading.

## Configuration migrations and deprecations

Each configuration construct ww deprecates or removes is one migration,
declared in `ww.migrations.config` and listed for operators in the upgrade
guide, which is generated from those declarations: `ww docs upgrade` prints
it, `ww docs upgrade#<section>` the section every message about a construct
names, and [deprecations.md](deprecations.md) is the same text. A migration
names the version that deprecated the construct and, once scheduled, the
version that removes it; why it changed; its section of the upgrade guide;
the files it can appear in; where it is; and how to change it. It applies to
every kind of configuration file: `ww.yaml`, the files it imports,
`ww.local.yaml` and the user's `ww.yaml`; the frontmatter of rule files;
and the settings files, `ww.json`, `ww.local.json`, the user's `ww.json` and
a configured project's own.

A migration is one of three kinds:

- **automatic**: declarative changes turn the old shape into the new one;
- **choice**: several options each change it differently, with consequences
  the operator weighs. Until the operator chooses, ww applies the first
  option, which keeps the old behaviour;
- **manual**: options describe what the operator can do, and ww changes
  nothing itself.

Migrations find only what is still in the old shape, so applying one twice
changes nothing the second time. No ledger records which ran; they run in
the order releases added them.

While a construct is deprecated, ww still reads it. Every file is migrated
in memory as it is read, before it is validated, so the parser and the
engine only ever see the current shape; the file itself is not touched. The
command then warns, once per place, naming the file, the path to the
construct and its line:

```text
ww warning: Deprecated: ww.yaml:workflows[0].steps[0].assess.question (line 6): an assessment's prompt key question is now assess (deprecated in ww 1.0.0b12); run ww migrate to fix. See ww docs upgrade#assessment-prompt-key-question.
```

Every command but `ww lint`, which lists them instead, warns about the
workflow files and the settings files; an extension's settings warn once the
command loads the extension. Rule files are migrated as they are read without
a warning, and listed by `ww lint`. A file that holds a construct in both its
old and its new shape is an error, since ww cannot tell which one is meant.

From the version that removes a construct, ww no longer migrates it in
memory: a file that still holds it is an error that names the removal and
the upgrade guide, rather than the parser's bare unknown key. A prerelease
of that version, such as `2.0.0b1` for a removal in `2.0.0`, counts as it,
since the removal lands in its first prerelease:

```text
ww error: ww.yaml:workflows[0].steps[0].assess.question (line 6): an assessment's prompt key question is now assess; removed in ww 2.0.0: run ww migrate to fix. See ww docs upgrade#assessment-prompt-key-question.
```

That detector stays until the next major version after the removal; from
then on the migration is forgotten. A removal only ever lands in a major
version once ww is at 1.0.

`ww lint` lists every deprecated construct the project's files still hold,
one `Deprecated:` line each, marked `[auto-fixable by ww migrate]`,
`[choose with ww migrate]` or `[manual]`. A construct ww cannot fix in the
file itself is marked manual even when its migration is automatic, and its
line says why and what to change by hand.

`ww migrate` applies these migrations to the files (see
[The ww migrate command](#the-ww-migrate-command)). It edits a
YAML file in place, at the characters each change concerns, so comments,
blank lines, quoting and indentation elsewhere stay exactly as they were.
It leaves a change to the operator, with the line and what to change, where
an edit could not be made without guessing: a flow mapping or list
(`{...}`, `[...]`) at the change, an anchor or alias on the way to it or
inside what changes, a merge key (`<<`), a block scalar (`|`, `>`) that would
move or be replaced, a key that is the only one of its mapping, and a file
with more than one YAML document. After a migration's edits, the file is
parsed again and must read exactly as the in-memory migration reads the
original; if it does not, none of that migration's edits is kept. A JSON
settings file is parsed, migrated and written again as ww writes `ww.json`:
two-space indentation and a final newline, so its own formatting is not
kept.

ww has one configuration migration today, `assess-question`; see
[deprecations.md](deprecations.md).

### The ww migrate command

`ww migrate` finds every deprecated or removed construct in the project's
configuration files: `ww.yaml`, its imports, `ww.local.yaml` and the user's
`ww.yaml`; the rule files the configuration's own groups and steps name; and
`ww.json`, `ww.local.json`, the user's `ww.json` and each configured
project's own, with the settings migrations of every configured extension.
It reads and edits the checkout's files, never a configuration snapshot.

- Without a flag it is a dry run: each construct with its file and line, its
  kind (`automatic`, `choice`, `manual` or `removed`), whether
  `ww migrate --apply` fixes it, and the message naming its guide section;
  then a unified diff of every file it would change. Nothing is written.
- `--apply` writes every changed file together, or none, then lists what it
  changed and what is left.
- A choice migration changes nothing until the operator picks an option.
  At a terminal, `--apply` shows each one's places and options with their
  consequences and asks; Enter leaves it. Elsewhere, and in any mode,
  `--choose <migration>=<option>` picks one (repeatable; an extension's
  migration is named `<extension>:<id>`). ww never picks an option itself:
  without a choice the construct is listed under "To choose" with the
  `--choose` to pass. An unknown migration or option is an error and nothing
  is written.
- What ww cannot change is listed under "Left for you to change by hand",
  each with its file and line, why, what to change, the options and their
  consequences when the migration has some, and its guide section.
- `--check` changes nothing and exits 1 while a removed construct is left.
  With `--deprecations` it exits 1 while any construct is left, deprecated
  or removed, whether ww can fix it or not; otherwise deprecated constructs
  are listed but never fail it.
- `--json` prints one document: `mode` (`dry_run`, `apply` or `check`);
  `unloaded`, why the configuration or the settings did not load; `findings`,
  each with `file`, `line`, `migration`, `kind`, `location`, `message`,
  `fixed`, `status` (`fixable`, `fixed`, `choose` or `manual`), `options`
  (`name`, `consequence`) and `guide`; `files`, each file it changes or
  would change with `file`, `path`, `kind` and `diff`; `choices`, each
  choice still open with `migration`, `why`, `options`, `places` and
  `guide`; `chosen`, the options passed or picked; `written`, the files
  `--apply` wrote; and with `--check`, `check` (`passed`, `deprecations`,
  `failing`, the number of constructs that fail it).

It exits 0 otherwise, also when constructs are left to the operator, and 1
for an error.

The command runs before ww loads the configuration or any extension's
settings, since a removed construct is exactly what stops them loading.
When the configuration does not load, it says why and finds the files from
the files themselves: the imports each workflow file lists, the rule files
and directories its rule groups and steps name that exist, the projects the
settings files list, and the extensions their `extensions` sections name.

`ww migrate --tasks` concerns stored task state instead. ww reads what an
older ww wrote anyway; this rewrites every task and child task in the
current format at once, before an older format stops being readable or to
stop migrating closed tasks on every read. Without `--apply` it lists each
task whose index or run documents an older format holds, and the tasks that
do not read. With `--apply` it takes each task's lock, copies its state
files (the index and the run documents it names) to
`.ww/backups/tasks-<UTC time>/<task ID>/`, at their paths below the task's
directory, and commits the task again, which writes the current format and
`written_by`. A task already current is not touched, `written_by` alone
never makes one outdated, and a task that does not read is reported and
left alone. Copying a backup back over `.ww/tasks/` restores the tasks.
`--json` prints `mode`, `tasks` (`task_id`, `files`, `rewritten`),
`unreadable` (`task_id`, `error`) and `backup`. `--tasks` takes only
`--apply` and `--json`.

### Writing a configuration migration

A deprecation lands as a `ConfigMigration` appended to `CONFIG_MIGRATIONS` in
`src/ww/migrations/config/catalog.py`, in the release that deprecates the
construct. Its upgrade guide section is generated from it, with the
optional `title`, `notes` and `example` (a before and an after excerpt);
`scripts/generate_upgrade_guide.py` writes the guide to
`documentation/deprecations.md`, and a test fails until the file matches.
The engine, the parser and the models change to the new shape only; the old
one lives in the migration and nowhere else.

```python
ConfigMigration(
    id="assess-question",
    deprecated_in="1.0.0b12",
    removed_in=None,  # a version once the removal is scheduled
    why="an assessment's prompt key question is now assess",
    anchor="assessment-prompt-key-question",
    files=("workflows",),
    changes=(
        rename_key("**.assess.question", "assess"),
        rename_key("**[name=assess].question", "assess"),
    ),
)
```

`files` takes `workflows`, `rules` and `settings`. A change addresses its key
with a key path: mapping keys joined by dots, `[*]` for every item of a
list, `*` for every key of a mapping, `**` for any depth (steps nest in
steps, hooks, handlers, items, children and assessment branches, so a step
key is usually found with `**`), and `[key=value]` to keep only mappings
whose `key` is `value`. The path ends with the key the change concerns.

The four changes are `rename_key(path, to)`, `delete_key(path)`,
`set_value(path, {old: new})` (only the old values named change) and
`move_key(path, to, up=0)`, which moves a key to the dotted `to` below the
mapping that holds it, after going `up` mappings out, creating missing
mappings on the way. Each declaration gives both the in-memory change and
the text edits, so they cannot disagree; the re-parse check catches the
rest. A change none of these express subclasses `Change` and implements
`apply` (the in-memory change) and `instruction` (what the operator does by
hand), and `edit` when ww can make it in the text; without `edit` it is
left to the operator in YAML files and applied to JSON ones.

A choice declares `options`, each an `Option(name, consequence, changes)`,
the first keeping today's behaviour. A manual migration declares `options`
without changes and `detect`, the key paths that find the construct.

The API `ww migrate` and `ww lint` use is in the same package:
`migrate_file(ConfigFile(path, kind, label), choices=...)` returns a
`FileMigration` with the file's new text, its `diff()` and its `findings`
(`Finding`: file, 1-based line, migration ID, kind — `automatic`, `choice`,
`manual` or `removed` — the path, the message, and whether the new text
fixes it); `write_files` writes several together or none; and
`ww.migrations.config.project.project_files` names a project's files, and
`migrate_project` migrates them all for `ww migrate`, finding them without
loading the configuration when it does not load.
`migrate_loaded` is the in-memory form every loader calls.

## Extensions

`EXTENSION_API_VERSION` (currently 1) changes only with a breaking change to
the extension API. ww loads any extension whose `api_version` lies between
`MIN_SUPPORTED_EXTENSION_API_VERSION` and `EXTENSION_API_VERSION`, both
exported by `ww.extensions`. An extension that needs a newer API fails with
"upgrade ww"; one written for an API ww no longer supports fails with "upgrade
the extension, or finish its tasks with the ww version it was written for".

An extension that renames or reshapes one of its own settings declares
`settings_migrations` on its `Extension`: the same `ConfigMigration` values,
with `files=("settings",)`, key paths that start inside the extension's own
section, `deprecated_in` and `removed_in` that are versions of the
extension, and a `guide` of its own. This is an additive part of the API and
changes no API version. ww reads them only once it loads the extension,
which it does when the configuration uses it, so an installed extension the
project never references is not imported for its migrations. When ww hands
the extension its settings, it migrates each settings file's section before
merging them, warning about each as for ww's own settings; it migrates the
copy a saved plan froze each time it uses it, without rewriting the run; and
`ww lint`, which loads every configured extension, lists what is left to
change in the files. From `removed_in` the old setting is an error naming the
removal, for a saved plan's copy too, which stops only runs whose plan holds
it.

Deprecated API features are declared in `ww/extensions/deprecations.py`
(`DeprecatedApiFeature`: the feature, `deprecated_in`, `removed_in`, the
replacement and a detector). They keep working until the API version that
removes them; an extension that uses one loads with a warning:

```text
ww warning: Deprecated: extension '<id>' (<source>) uses <feature>, deprecated in ww X and removed in ww Y; use <replacement> instead.
```

`ww lint` lists the same warnings as `Notice:` lines. Nothing is deprecated
today.

An extension's own `version` follows semantic versioning; its major version
is the leading number. An open run whose plan uses the extension takes a new
minor or patch version, API version or source silently at the manager's next
`next`. A new major version stops the run with `plan_changed`: `--keep-plan`
continues the saved plan under the new version, and `--replan` reruns the
steps and hooks that use it. A `0.x` version counts only its leading `0`, and
a non-numeric version counts every change as major. Extensions the plan does
not use are never loaded for this check.

Extension authors bump the major version only for changes that break saved
runs (removed handlers, changed outputs or arguments, changed meaning);
additive changes are minor or patch releases.

## Configuration snapshots

A run reads the configuration of the commit it started with, from a
read-only copy under `.ww/config/<sha>/`. That copy is never rewritten, by
`ww migrate` or anything else: it is what the commit holds. It is read
through the same loaders as the checkout, so every configuration migration
in its deprecation window applies to it in memory as it loads, and an open
run whose pinned commit predates a deprecation keeps working. A snapshot
holding a construct that a newer ww has removed fails to load like any
other file; the run then keeps to its saved plan, as when a snapshot cannot
be read for any other reason, until `ww config refresh` moves it to a commit
that has been migrated.

## Reading tasks ww cannot decode

A task this ww cannot read in full, such as a closed task an older ww wrote,
still opens in `ww visualize`, showing less. The page then reads only the
task's envelope, which no schema version renames: the task ID, `written_by`
(absent from files written before ww recorded it) and, for each run, its ID,
workflow, status, `created_at` and `updated_at`. It reads them with plain
JSON, never with the schema decoders, and shows a missing value as unknown.
The list marks such a task `older format (schema N): reduced view` (or `newer
format …` when a newer ww that recorded its version wrote it; any other reason
gets a plain `reduced view` badge with ww's reason). Its detail shows each
run, the Markdown artifacts under `runs/<run_id>/steps/` (opened like any
artifact; only those files are served), its commits, metadata and direct
work. The plan, step statuses, rules and extension values need the full
record and are left out. The envelope's names are a promise: they are never
renamed, so every future ww can show every past task this way.

## Removed constructs in running tasks

A run executes the plan it saved when it started, so a construct ww stops
running, such as an action or a kind of plan item, can sit in a run an older
ww started. Such a removal is a `RemovedConstruct` in
`src/ww/migrations/removals.py`: an ID, what it is, the versions that
deprecated and removed it, `last_version`, the newest ww that still runs it,
and a detector. The detector reads the plan snapshot as the run document
stores it, expanded but not decoded, because decoding the construct is what
a ww without it can no longer do; `plan_items` walks a snapshot's plan and
template items, the verification items nested in them included. ww has
removed no plan construct yet, so the list is empty.

ww refuses a run, not a version. When it reads a run document, it runs every
detector whose `removed_in` this ww has reached before decoding the plan,
and a run whose plan holds the construct is refused with a message that
names it and how to finish:

```text
ww error: task 'TASK-5': its open run '01-task' uses the `legacy` action (plan.items[3] (review)), removed in ww 2.0.0, which this ww no longer runs: finish it with ww 1.9.3 (`pipx install --force ww-agentic-workflows==1.9.3`), or discard the task with `./ww reset TASK-5`. Other tasks are unaffected.
```

Every other task carries on: commands addressing other tasks work, and scans
across tasks, such as `discover` and `ww visualize`, list the refused task as
one ww cannot read, the visualizer in its reduced view. A task is read
whole, so a construct in one of a task's finished runs refuses that task
too; its message says so and names the version to read it with. `ww reset`
removes such a task without reading it.

## Upgrading across a major version

A removal lands only in a major version, and only once a release of the
major version before it has deprecated the construct with its `removed_in`
set: a configuration migration with `removed_in`, or a `RemovedConstruct`.
So the last release of major version N knows every removal N + 1 makes, and
the installed ww can judge a project against a version whose code it has
never seen.

`ww upgrade` does that before installing a version whose major number is
higher than the installed one's. It first works out which version it is
about to install: for a PyPI installation, the newest compatible release,
the one pip or pipx selects; for an editable Git checkout, the release series
`pyproject.toml` names on the tracking branch, which it fetches before
fast-forwarding to exactly that commit. Then, with this build's own lists,
it collects:

- every finding of ww's own configuration migrations in the project's files
  (as `ww migrate` reads them) whose migration is removed already, or whose
  `removed_in` the target reaches, a prerelease counting as the release it
  precedes;
- every run of a task in progress whose saved plan holds a construct the
  target removes, and every open run this ww refuses already.

If anything is found, the upgrade is refused with the list, what to do (run
`ww migrate`, or the `ww-migrate` workflow; finish or `reset` the tasks) and
`--force`, which installs the version anyway and prints the list after it.
A minor or patch release is never held back. After any successful upgrade in
a ww project, the upgraded ww checks the configuration in a new process, as
`ww migrate --check --deprecations`, and offers `ww migrate` and the
`ww-migrate` workflow when there is something to change; it changes
nothing itself.

The check has limits, and says so where it meets one:

- It knows the removals the installed build knows. A release of N older than
  the last one may lack a deprecation added later in N, so upgrade to the
  last release of your major version first, then across.
- It needs the target version. Offline, or in a checkout whose upstream has
  no readable `pyproject.toml`, the upgrade goes ahead and says the check did
  not run. A checkout's `pyproject.toml` names the release series, not the
  exact number CI will release, which is enough to tell the major version.
- It checks the project the command runs in, not every project the same
  installation serves.
- Extension settings migrations are judged against the extension's own
  version, which a ww upgrade does not change, so they are not part of it.
- Finished tasks are not listed: nothing runs them again, and the
  visualizer still shows them in its reduced view.

## The ww-migrate workflow

`ww-migrate` is a bundled workflow, listed by `ww workflows` beside ww's
other ones, that brings the project's configuration up to date with the
installed ww together with the operator:

1. `detect` runs `ww migrate --json` and `ww migrate --check --deprecations`
   and lists every finding by kind, changing nothing;
2. `apply` runs `ww migrate --apply`, which makes only the automatic fixes;
3. `resolve`, an interactive step, goes through every choice, manual and
   removed finding with the operator: why it changed, the options and their
   consequences, and the change the agent proposes. The operator decides
   each one; a chosen option is applied with `ww migrate --apply --choose
   <id>=<option>`, and an agreed manual change is made by the agent at the
   place the finding names;
4. `verify` runs `ww migrate --check --deprecations` and `ww lint` again and
   reports what is left.

It runs in the current checkout with `runtime: single` and is
`restartable`. Like the setup workflows, it creates no branch, worktree or
commit: the changed configuration files are left uncommitted for the
operator to review and commit. Start it as any workflow, for example
`ww start MIGRATE-1 --workflow ww-migrate --agent claudecode`.
