---
name: ww-wizard
description: Help the operator create a ww workflow, change an existing one, create or improve rules, or choose an approach, adapting the questions to the request and placing every change through ww's own commands. Use when the operator invokes /ww-wizard, or asks to design, change or tune how ww works in this project and has not said exactly what to edit.
---

# Shape ww's setup with the operator

Talk in plain words and ask only what changes the outcome. This skill
orchestrates: ww's design documents decide what a setup can be made of, ww's
commands validate and place every change, and the rules skills write rules.
Never edit ww's configuration files yourself.

## Start

1. Run `./ww discover`. If ww is disabled, say so and stop. Read the
   operator's request.
2. If it already says what to do, go straight to that branch. Otherwise ask
   which one, as a single choice through the host's choice tool where
   available (a numbered list in the chat otherwise), and end your turn until
   the operator answers:
   - Create a workflow.
   - Change an existing workflow.
   - Create or improve rules.
   - Help me choose an approach.
3. Match the depth of your questions to the request: a precise request gets
   one confirming question at most; a vague one gets the few questions it
   needs, one message at a time. A timeout or a preselected value is not an
   answer.

Before drafting anything, read the design authorities the branch needs, which
every installation prints: `./ww docs features` (start at "Designing a
workflow"), `./ww docs specification` for exact syntax and `./ww docs
examples` for the shape of a setup. Inspect the project: `.ww/project.md` when
it exists, the project's own commands and CI, and the workflows already
configured. Take a test, lint or type command only from that evidence and say
where it comes from; where it is missing or contradictory, ask instead of
guessing.

## Create a workflow

Ask for the trigger, the result wanted and where the operator wants to be
involved. Choose the smallest structure that expresses it, adding items,
assessments, modes or reusable groups only for a concrete requirement
the features guide gives a reason for. Challenge anything the request does not
need: name a simpler alternative and what it costs the operator, and let them
decide; do not walk through every property. Then show concise YAML and a short
walkthrough of a representative task, with a failure and retry path when an
external effect or an automatic command is involved. Write the draft as a
setup fragment (root keys `workflows`, `modes`, `documents`, `handlers`,
`hooks`, `rules`, plus `settings`) to a file outside the repository, such as in
your scratch directory.

## Change an existing workflow

1. Find where it is defined: `./ww discover --json` gives each workflow's
   `source` file and `source_level` (`local`, `project` or `global`). State
   which definition is in force and which level it is at.
2. Draft the complete changed workflow as a fragment whose `workflows` list
   holds that one entry. Keep what the operator did not ask to change.
3. Edit it where it is written: `./ww setup update <name> <fragment>`, adding
   `--level <source_level>` so that ww refuses rather than edit a definition
   another level hides. If the operator wants a change for themselves only to a
   shared workflow, say that this is an override that hides the shared
   definition for them alone, and place it with `./ww setup apply <fragment>
   --for me` only when they confirm. Never create such an override silently,
   and never hide a shared edit under a local one.
4. A workflow ww ships (the `ww-*` ones) is not edited: a same-named
   definition placed with `setup apply` replaces it.

## Create or improve rules

Hand the work to the rules skills instead of writing rules yourself: `ww-rule`
for rules in the operator's own words, `ww-rules-from-artifacts` for lessons
that recur in past results, `ww-feedback-rules` for learned operator feedback,
and `ww-scriptize` to give rules that have none a check. Say which one fits and
why, then follow it.

## Help me choose an approach

Ask what goes wrong or what the operator wants to be easier, then lay out two
or three options in plain words, each with what it asks of the operator and
what it costs: a rule, a check, a mode, a small workflow, or doing nothing.
Recommend the simplest that meets the need. When they pick one, continue in its
branch.

## Validate, show, apply

1. Validate and inspect the draft before asking anything:
   `./ww setup apply <fragment> --for <me or team> --dry-run --inspect <workflow>
   --agent <your agent>` for new definitions, `./ww setup update <name>
   <fragment> --dry-run --inspect <workflow> --agent <your agent>` for a
   change. In the compiled plan, check that automation is ww-owned (commands
   run as handlers or hooks, never as a step asking the agent to run them),
   that item scopes are valid, and that the operator meets only the
   conversations they intended. Fix the draft and validate again until it is
   clean.
2. Show the YAML or the diff, the walkthrough, and the dry run's list of
   changes. For a new definition, ask whether it is for the operator alone
   (`--for me`, local files kept out of version control) or the team
   (`--for team`, shared files).
3. When the operator approves the shown edit, apply exactly it with `--yes`;
   do not ask again. If ww refuses, show its message and stop; never place the
   configuration another way.
4. Run `./ww lint`, tell the operator which files changed, and that shared
   ones are left uncommitted for them to review and commit.

For items, place normal work in `items.steps`; it runs once over the collection.
Use item-field saves for every record and explicit resolve/report transitions.
Bare items have an empty body and still require all obligations completed at
their container boundary.
