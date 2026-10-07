---
name: ww-rule
description: Add, amend, move, or re-scope the rules ww gives to workflow steps, from the operator's own words. Use when the user invokes /ww-rule, asks to add or change a rule, a convention, or a check for ww's steps, or asks to turn a document (/ww-rule split <file>) or review findings (/ww-rule from-review) into rules.
---

# Write ww rules from the operator's words

A rule is one sentence a step's agent must follow, in a Markdown file whose
optional frontmatter scopes it to files (`paths`) or gives it a command
(`check`). You decide what the rules are; `./ww rules add`, `edit`, `move`,
`filter` and `promote` write them, validated. Never edit a rule file, a
group, `ww-rules.yaml`, `ww.yaml` or
`ww-rule-automation.json` yourself.

1. **Learn what exists.** Run `./ww rules --json`: groups, their filters and
   directories' rules with IDs, summaries, globs, the project's
   `check_guidance` setting, and `targets`, every workflow's steps with the
   `path` a `steps` filter names to reach exactly that step (loop bodies and
   per-item stages included) and whether the step is `agent_owned`; only
   such a step takes rules. Use only workflow names and step names or paths
   it shows; never invent one.
2. **Split into atomic obligations.** One rule is one thing an agent can do
   or fail to do. Break the input into such obligations and merge the ones
   that say the same thing twice. For each, search the existing rules by ID
   and by wording (the `summary` fields) and classify it: **new**, an
   **amendment** of `<id>` (same obligation, new wording or globs), or a
   **filter change** of `<group>` (the rule is right, the steps it reaches
   are not).
3. **Decide globs.** Give `paths` only when the sentence names a kind of file
   or a directory. Count what each glob matches (`git ls-files | grep -c`,
   or `--dry-run`, which reports the count). A glob that matches nothing is
   dropped, and you say so.
4. **Decide placement from the real filters.** An existing group whose
   `workflows`/`steps` fit the rule, whether it lists a directory or its
   files one by one (`rules add` then writes the file beside the group's
   last one, or in `--dir`, and lists it; a group naming only other groups
   needs `--dir`); else propose a new group with its directory and filters;
   else, for a one-off, the step's own `rules:` list, which the operator
   edits in the YAML by hand (say exactly what to add).
5. **Rewrite each rule** as one imperative sentence with concrete nouns and
   no hedging; the rationale and an example go in the body below it, since
   the step page shows only the first sentence. Propose a `check` only when
   it is obvious: a one-line shell command, or an existing tool whose
   configuration the rule plainly belongs to, listed among the store's
   `checks`. Write a check for the directory ww runs it from, the step's
   directory (the task's worktree when there is one), through the wrapper
   the project runs its own commands with, such as a container exec, as
   `.ww/project.md` or the agent instructions record it; never an absolute
   path into the main checkout. When `check_guidance` is set, follow it; it
   wins over these defaults. Otherwise leave it without one: a verifier
   judges it until `ww-scriptize-rules` (the `ww-scriptize` skill) builds a
   check for it with the operator.
6. **Confirm once.** Show one block with, per rule: ID, group, the group's
   filters, glob and its match count, the sentence, and `new` or
   `replaces <id>: <old sentence>`. For an amendment of a rule with an
   approved store command (`store_check` in `./ww rules --json`), say
   whether you will **promote** that command first, the default when the
   meaning is unchanged (`./ww rules promote <check>`, then edit), or let
   `ww-scriptize-rules` build one again for the new wording, since changing
   the wording stops the stored command from matching. Wait for the operator's answer; change nothing
   before it.
7. **Write only through the CLI**, in this order: new groups
   (`./ww rules add --group <name> --dir <path> [--workflows ...] [--steps ...]`),
   promotions (`./ww rules promote <check>`), amendments
   (`./ww rules edit <id> [--text "<sentence and body>"] [--paths <glob> ...]`),
   moves (`./ww rules move <id> <group>`), filter changes
   (`./ww rules filter <group> [--workflows ...] [--steps ...]`), new rules
   (`./ww rules add <group> --text "<sentence and body>" [--paths <glob> ...]
   [--assert empty|equals:<v> ...] [--id <stem>]
   [--check-shell "<sh>" | --check-argv -- <arg> ...]`; `--check-argv --` goes
   last, so the checked tool's own options stay its own). Each command refuses a write that would leave the
   configuration invalid and changes nothing then; `--dry-run` checks one
   first. A refusal is reported to the operator, not worked around.
8. **Show the result.** Run `./ww lint` and show its output, and show where
   each rule now applies: the steps each write command lists, or
   `./ww rules`. Nothing is committed; say which files changed.

## Variants

- `/ww-rule split <file>`: every bullet or numbered item of a prose document
  is a candidate rule; group the rules by the document's own sections into
  one group per section, with the steps each section concerns, and confirm
  the whole batch in one block as in step 6. Leave the document as it is
  unless the operator asks to replace it with a pointer to the groups.
- `/ww-rule from-review`: read the last review artifact or fix page of the
  task the operator names (`./ww artifacts <task>`), and turn each finding
  that states a lasting convention, not a one-time defect, into a candidate
  rule; then continue from step 2.
