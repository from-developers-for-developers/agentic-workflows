# Deprecations and upgrades

This guide lists the configuration ww still reads but is scheduled to
change, and the configuration it no longer reads. It is generated from ww's
configuration migrations: `ww docs upgrade` prints it, and
`ww docs upgrade#<section>` prints the one section that every message about
a construct names. No removal is scheduled unless a section names its
version.

ww reads a deprecated construct by migrating it in memory as it loads each
file, and warns, naming the file, the path to the construct and its line:

```text
ww warning: Deprecated: <file>:<path> (line <n>): <why> (deprecated in ww <version>); run ww migrate to fix. See ww docs upgrade#<section>.
```

Imported, user-level, local and inherited configuration is included, and so
are rule files' frontmatter and the settings files. `ww lint` lists every
construct left, marked `[auto-fixable by ww migrate]`, `[choose with ww
migrate]` or `[manual]`. Once a construct is removed, a file still holding it
is an error naming the removal and its section here. Using both the old and
the new spelling in one place is an error.

## Upgrading

1. `ww migrate` shows what it would change: every construct it finds, with
   its file and line, and a diff of every file it would edit. It changes
   nothing.
2. `ww migrate --apply` makes the automatic changes, editing each file in
   place so its comments and formatting stay. A construct with several
   options is changed once you choose one: at a terminal `ww migrate --apply`
   asks; elsewhere pass `--choose <migration>=<option>`. ww never chooses for
   you.
3. What ww cannot change is left to you. `ww migrate` names each place, why
   it changed, the options and their consequences, what to edit, and its
   section below.
4. `ww migrate --check` exits non-zero while a removed construct is left, and
   `ww migrate --check --deprecations` while anything is.

Stored task state needs no migration: ww reads what an older ww wrote.
`ww migrate --tasks --apply` rewrites it in the current format anyway, after
copying it to `.ww/backups/`.

<a id="assessment-prompt-key-question"></a>

## Assessment prompt key `question`

Migration `assess-question`: deprecated in ww 1.0.0b12; no removal is scheduled. Found in workflow files (`ww.yaml`, the files it imports, `ww.local.yaml` and the user's `ww.yaml`).

Why: An assessment's prompt key question is now assess.

Use `assess` inside the full assessment mapping. The compact `assess: <prompt>` form remains supported. The same applies inside an assessment attached to a regular step, and to the explicit standalone `name: assess` form, whose `question` field becomes `assess` too.

Automatic: `ww migrate --apply` makes the change:

- rename `**.assess.question` to `assess`
- rename `**[name=assess].question` to `assess`

Before:

```yaml
- assess:
    question: Are there issues?
    positive:
      handler: fix-findings
```

After:

```yaml
- assess:
    assess: Are there issues?
    positive:
      handler: fix-findings
```
